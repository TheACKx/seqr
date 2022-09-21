from django.urls.base import reverse
import mock

from seqr.views.apis.superuser_api import get_all_users
from seqr.views.utils.test_utils import AuthenticationTestCase, AnvilAuthenticationTestCase, USER_FIELDS

ALL_USERS_USER_FIELDS = {'hasGoogleAuth'}
ALL_USERS_USER_FIELDS.update(USER_FIELDS)
ALL_USERS_USER_FIELDS -= {'firstName', 'lastName', 'isAnvil'}

EXPECTED_USERS = {
    'test_user_manager', 'test_user_collaborator', 'test_user_no_access', 'test_user', 'test_local_user',
    'test_superuser', 'test_data_manager', 'test_pm_user', 'test_user_inactive', 'test_user_no_policies',
}


class SuperusersAPITest(object):

    @mock.patch('seqr.views.utils.permissions_utils.PM_USER_GROUP')
    @mock.patch('seqr.views.utils.permissions_utils.ANALYST_USER_GROUP')
    def test_get_all_users(self, mock_analyst_group, mock_pm_group):
        mock_analyst_group.__bool__.return_value = False
        mock_pm_group.__bool__.return_value = False

        url = reverse(get_all_users)
        self.check_superuser_login(url)

        response = self.client.get(url)
        self._test_superuser_response(response)

        mock_analyst_group.__bool__.return_value = True
        mock_analyst_group.resolve_expression.return_value = 'analysts'
        mock_analyst_group.__str__.return_value = 'analysts'
        response = self.client.get(url)
        self._test_superuser_response(response, analyst_enabled=True)

        mock_pm_group.__bool__.return_value = True
        mock_pm_group.resolve_expression.return_value = 'project-managers'
        mock_pm_group.__str__.return_value = 'project-managers'
        self._test_pm_users(url)

    def _test_pm_users(self, url):
        response = self.client.get(url)
        self._test_superuser_response(response, analyst_enabled=True, pm_enabled=True)

    def _test_superuser_response(self, response, analyst_enabled=False, pm_enabled=False):
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertSetEqual(set(response_json.keys()), {'users'})
        users_by_username = {user['username']: user for user in response_json['users']}
        self.assertSetEqual(set(users_by_username.keys()), EXPECTED_USERS)

        pm_user = users_by_username['test_pm_user']
        self.assertSetEqual(set(pm_user.keys()), ALL_USERS_USER_FIELDS)
        self.assertEqual(pm_user['hasGoogleAuth'], self.HAS_GOOGLE_AUTH)
        self.assertEqual(pm_user['isPm'], pm_enabled)
        self.assertEqual(pm_user['isAnalyst'], analyst_enabled)

    def test_admin(self):
        url = 'http://localhost/admin/'
        self.check_superuser_login(url, login_redirect_url='/admin/login/', policy_redirect_url='/admin/login/',
                                   permission_denied_error=302)

        response = self.client.get(url)
        self.assertContains(response, 'Django administration', status_code=200)


class LocalSuperusersAPITest(AuthenticationTestCase, SuperusersAPITest):
    fixtures = ['users']
    HAS_GOOGLE_AUTH = False


class AnvilSuperusersAPITest(AnvilAuthenticationTestCase, SuperusersAPITest):
    fixtures = ['users', 'social_auth']
    HAS_GOOGLE_AUTH = True

    def _test_pm_users(self, url):
        mock_analyst_group = self.mock_get_group_members.call_args.args[1]
        self.assertEqual(str(mock_analyst_group), 'analysts')
        self.mock_get_group_members.assert_called_with(self.super_user, mock_analyst_group, use_sa_credentials=True)
        self.assertEqual(self.mock_get_group_members.call_count, 1)

        # Test the case where the superuser does not have access to the PM group in AnVIL
        # In that case, the request should succeed but not populate any PM users
        self.mock_get_group_members.reset_mock()
        response = self.client.get(url)
        self._test_superuser_response(response, analyst_enabled=True, pm_enabled=False)
        mock_pm_group = self.mock_get_group_members.call_args.args[1]
        self.assertEqual(str(mock_pm_group), 'project-managers')
        self.mock_get_group_members.assert_has_calls([
            mock.call(self.super_user, mock_analyst_group, use_sa_credentials=True),
            mock.call(self.super_user, mock_pm_group),
        ])
        self.assertEqual(self.mock_get_group_members.call_count, 2)

        # Test if the superuser does have access to the PM group it populates properly
        self.mock_get_group_members.reset_mock()
        self.mock_get_group_members.side_effect = lambda *args, **kwargs: [self.pm_user.email]
        super(AnvilSuperusersAPITest, self)._test_pm_users(url)
        self.mock_get_group_members.assert_has_calls([
            mock.call(self.super_user, mock_analyst_group, use_sa_credentials=True),
            mock.call(self.super_user, mock_pm_group),
        ])
        self.assertEqual(self.mock_get_group_members.call_count, 2)
