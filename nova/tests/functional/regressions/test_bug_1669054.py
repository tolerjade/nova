# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from nova import context
from nova import objects
from nova.tests.functional import integrated_helpers
from nova.virt import fake


class ResizeEvacuateTestCase(integrated_helpers._IntegratedTestBase,
                             integrated_helpers.InstanceHelperMixin):
    """Regression test for bug 1669054 introduced in Newton.

    When resizing a server, if CONF.allow_resize_to_same_host is False,
    the API will set RequestSpec.ignore_hosts = [instance.host] and then
    later in conductor the RequestSpec changes are saved to persist the new
    flavor. This inadvertently saves the ignore_hosts value. Later if you
    try to migrate, evacuate or unshelve the server, that original source
    host will be ignored. If the deployment has a small number of computes,
    like two in an edge node, then evacuate will fail because the only other
    available host is ignored. This test recreates the scenario.
    """
    # Set variables used in the parent class.
    REQUIRES_LOCKING = False
    ADMIN_API = True
    USE_NEUTRON = True
    _image_ref_parameter = 'imageRef'
    _flavor_ref_parameter = 'flavorRef'
    api_major_version = 'v2.1'
    microversion = '2.11'  # Need at least 2.11 for the force-down API

    def test_resize_then_evacuate(self):
        # Create a server. At this point there is only one compute service.
        flavors = self.api.get_flavors()
        flavor1 = flavors[0]['id']
        server = self._build_server(flavor1)
        server = self.api.post_server({'server': server})
        self._wait_for_state_change(self.api, server, 'ACTIVE')

        # Start up another compute service so we can resize.
        fake.set_nodes(['host2'])
        self.addCleanup(fake.restore_nodes)
        host2 = self.start_service('compute', host='host2')

        # Now resize the server to move it to host2.
        flavor2 = flavors[1]['id']
        req = {'resize': {'flavorRef': flavor2}}
        self.api.post_server_action(server['id'], req)
        server = self._wait_for_state_change(self.api, server, 'VERIFY_RESIZE')
        self.assertEqual('host2', server['OS-EXT-SRV-ATTR:host'])
        self.api.post_server_action(server['id'], {'confirmResize': None})
        server = self._wait_for_state_change(self.api, server, 'ACTIVE')

        # Disable the host on which the server is now running (host2).
        host2.stop()
        self.api.force_down_service('host2', 'nova-compute', forced_down=True)

        # Now try to evacuate the server back to the original source compute.
        # FIXME(mriedem): This is bug 1669054 where the evacuate fails with
        # NoValidHost because the RequestSpec.ignore_hosts field has the
        # original source host in it which is the only other available host to
        # which we can evacuate the server.
        req = {'evacuate': {'onSharedStorage': False}}
        self.api.post_server_action(server['id'], req,
                                    check_response_status=[500])
        # There should be fault recorded with the server.
        server = self._wait_for_state_change(self.api, server, 'ERROR')
        self.assertIn('fault', server)
        self.assertIn('No valid host was found', server['fault']['message'])
        # Assert the RequestSpec.ignore_hosts is still populated.
        reqspec = objects.RequestSpec.get_by_instance_uuid(
            context.get_admin_context(), server['id'])
        self.assertIsNotNone(reqspec.ignore_hosts)
        self.assertIn(self.compute.host, reqspec.ignore_hosts)
