# Copyright (c) 2016-2017 Adobe Systems Incorporated.  All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import string

import ldap.controls.libldap

import user_sync.config
import user_sync.connector.helper
import user_sync.error
import user_sync.identity_type
from user_sync.error import AssertionException


def connector_metadata():
    metadata = {
        'name': LDAPDirectoryConnector.name
    }
    return metadata


def connector_initialize(options):
    """
    :type options: dict
    """
    connector = LDAPDirectoryConnector(options)
    return connector


def connector_load_users_and_groups(state, groups=None, extended_attributes=None, all_users=True):
    """
    :type state: LDAPDirectoryConnector
    :type groups: Optional(list(str))
    :type extended_attributes: Optional(list(str))
    :type all_users: bool
    :rtype (bool, iterable(dict))
    """
    return state.load_users_and_groups(groups or [], extended_attributes or [], all_users)


class LDAPDirectoryConnector(object):
    name = 'ldap'

    expected_result_types = [ldap.RES_SEARCH_RESULT, ldap.RES_SEARCH_ENTRY]

    def __init__(self, caller_options):
        caller_config = user_sync.config.DictConfig('%s configuration' % self.name, caller_options)
        builder = user_sync.config.OptionsBuilder(caller_config)
        builder.set_string_value('group_filter_format', '(&'
                                                        '(|(objectCategory=group)'
                                                        '(objectClass=groupOfNames)'
                                                        '(objectClass=posixGroup))'
                                                        '(cn={group})'
                                                        ')')
        builder.set_string_value('all_users_filter', '(&'
                                                     '(objectClass=user)'
                                                     '(objectCategory=person)'
                                                     '(!(userAccountControl:1.2.840.113556.1.4.803:=2))'
                                                     ')')
        builder.set_string_value('group_member_filter_format', '(memberOf={group_dn})')
        builder.set_bool_value('require_tls_cert', False)
        builder.set_string_value('string_encoding', 'utf-8')
        builder.set_string_value('user_identity_type_format', None)
        builder.set_string_value('user_email_format', '{mail}')
        builder.set_string_value('user_username_format', None)
        builder.set_string_value('user_domain_format', None)
        builder.set_string_value('user_identity_type', None)
        builder.set_int_value('search_page_size', 200)
        builder.set_string_value('logger_name', LDAPDirectoryConnector.name)
        host = builder.require_string_value('host')
        username = builder.require_string_value('username')
        builder.require_string_value('base_dn')
        options = builder.get_options()
        self.options = options
        self.logger = logger = user_sync.connector.helper.create_logger(options)
        logger.debug('%s initialized with options: %s', self.name, options)

        LDAPValueFormatter.encoding = options['string_encoding']
        self.user_identity_type = user_sync.identity_type.parse_identity_type(options['user_identity_type'])
        self.user_identity_type_formatter = LDAPValueFormatter(options['user_identity_type_format'])
        self.user_email_formatter = LDAPValueFormatter(options['user_email_format'])
        self.user_username_formatter = LDAPValueFormatter(options['user_username_format'])
        self.user_domain_formatter = LDAPValueFormatter(options['user_domain_format'])

        password = caller_config.get_credential('password', options['username'])
        # this check must come after we get the password value
        caller_config.report_unused_values(logger)

        logger.debug('Connecting to: %s using username: %s', host, username)
        if not options['require_tls_cert']:
            ldap.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_NEVER)
        try:
            connection = ldap.initialize(host)
            connection.protocol_version = ldap.VERSION3
            connection.set_option(ldap.OPT_REFERRALS, 0)
            connection.simple_bind_s(username, password)
        except Exception as e:
            raise AssertionException('LDAP connection failure: ' + repr(e))
        self.connection = connection
        logger.debug('Connected')
        self.user_by_dn = {}

    def load_users_and_groups(self, groups, extended_attributes, all_users):
        """
        :type groups: list(str)
        :type extended_attributes: list(str)
        :type all_users: bool
        :rtype (bool, iterable(dict))
        """
        options = self.options
        all_users_filter = options['all_users_filter']
        group_member_filter_format = options['group_member_filter_format']

        # for each group that's required, do one search for the users of that group
        for group in groups:
            group_dn = self.find_ldap_group_dn(group)
            if not group_dn:
                self.logger.warning("No group found for: %s", group)
                continue
            group_member_subfilter = group_member_filter_format.format(group_dn=group_dn)
            if not group_member_subfilter.startswith("("):
                group_member_subfilter = '(' + group_member_subfilter + ')'
            user_subfilter = all_users_filter
            if not user_subfilter.startswith("("):
                user_subfilter = '(' + user_subfilter + ')'
            group_user_filter = '(&' + group_member_subfilter + user_subfilter + ')'
            group_users = 0
            for user_dn, user in self.iter_users(group_user_filter, extended_attributes):
                user['groups'].append(group)
                group_users += 1
            self.logger.debug('Count of users in group "%s": %d', group, group_users)

        # if all users are requested, do an additional search for all of them
        if all_users:
            ungrouped_users = 0
            grouped_users = 0
            for user_dn, user in self.iter_users(all_users_filter, extended_attributes):
                if not user['groups']:
                    ungrouped_users += 1
                else:
                    grouped_users += 1
            self.logger.debug('Count of users in any groups: %d', grouped_users)
            self.logger.debug('Count of users not in any groups: %d', ungrouped_users)

        self.logger.debug('Total users loaded: %d', len(self.user_by_dn))
        return self.user_by_dn.itervalues()

    def find_ldap_group_dn(self, group):
        """
        :type group: str
        :rtype str
        """
        connection = self.connection
        options = self.options
        base_dn = options['base_dn']
        group_filter_format = options['group_filter_format']
        res = connection.search_s(base_dn, ldap.SCOPE_SUBTREE,
                                  filterstr=group_filter_format.format(group=group), attrsonly=1)
        group_dn = None
        for current_tuple in res:
            if current_tuple[0]:
                if group_dn:
                    raise AssertionException("Multiple LDAP groups found for: %s" % group)
                group_dn = current_tuple[0]
        return group_dn

    def iter_users(self, users_filter, extended_attributes):
        options = self.options
        base_dn = options['base_dn']

        user_attribute_names = ['givenName', 'sn', 'c']
        user_attribute_names.extend(self.user_identity_type_formatter.get_attribute_names())
        user_attribute_names.extend(self.user_email_formatter.get_attribute_names())
        user_attribute_names.extend(self.user_username_formatter.get_attribute_names())
        user_attribute_names.extend(self.user_domain_formatter.get_attribute_names())

        extended_attributes = list(set(extended_attributes) - set(user_attribute_names))
        user_attribute_names.extend(extended_attributes)

        result_iter = self.iter_search_result(base_dn, ldap.SCOPE_SUBTREE, users_filter, user_attribute_names)
        for dn, record in result_iter:
            if dn is None:
                continue
            if dn in self.user_by_dn:
                yield (dn, self.user_by_dn[dn])
                continue

            email, last_attribute_name = self.user_email_formatter.generate_value(record)
            email = email.strip() if email else None
            if not email:
                if last_attribute_name is not None:
                    self.logger.warning('Skipping user with dn %s: empty email attribute (%s)', dn, last_attribute_name)
                continue

            source_attributes = {}

            user = user_sync.connector.helper.create_blank_user()
            source_attributes['email'] = email
            user['email'] = email

            identity_type, last_attribute_name = self.user_identity_type_formatter.generate_value(record)
            if last_attribute_name and not identity_type:
                self.logger.warning('No identity_type attribute (%s) for user with dn: %s, defaulting to %s',
                                    last_attribute_name, dn, self.user_identity_type)
            source_attributes['identity_type'] = identity_type
            if not identity_type:
                user['identity_type'] = self.user_identity_type
            else:
                try:
                    user['identity_type'] = user_sync.identity_type.parse_identity_type(identity_type)
                except AssertionException as e:
                    self.logger.warning('Skipping user with dn %s: %s', dn, e)
                    continue

            username, last_attribute_name = self.user_username_formatter.generate_value(record)
            username = username.strip() if username else None
            source_attributes['username'] = username
            if username:
                user['username'] = username
            else:
                if last_attribute_name:
                    self.logger.warning('No username attribute (%s) for user with dn: %s, default to email (%s)',
                                        last_attribute_name, dn, email)
                user['username'] = email

            domain, last_attribute_name = self.user_domain_formatter.generate_value(record)
            domain = domain.strip() if domain else None
            source_attributes['domain'] = domain
            if domain:
                user['domain'] = domain
            elif last_attribute_name:
                self.logger.warning('No domain attribute (%s) for user with dn: %s', last_attribute_name, dn)

            given_name_value = LDAPValueFormatter.get_attribute_value(record, 'givenName')
            source_attributes['givenName'] = given_name_value
            if given_name_value is not None:
                user['firstname'] = given_name_value
            sn_value = LDAPValueFormatter.get_attribute_value(record, 'sn')
            source_attributes['sn'] = sn_value
            if sn_value is not None:
                user['lastname'] = sn_value
            c_value = LDAPValueFormatter.get_attribute_value(record, 'c')
            source_attributes['c'] = c_value
            if c_value is not None:
                user['country'] = c_value

            uid = LDAPValueFormatter.get_attribute_value(record, 'uid')
            source_attributes['uid'] = uid
            if uid is not None:
                user['uid'] = uid

            if extended_attributes is not None:
                for extended_attribute in extended_attributes:
                    extended_attribute_value = LDAPValueFormatter.get_attribute_value(record, extended_attribute)
                    source_attributes[extended_attribute] = extended_attribute_value

            user['source_attributes'] = source_attributes.copy()
            if 'groups' not in user:
                user['groups'] = []
            self.user_by_dn[dn] = user

            yield (dn, user)

    def iter_search_result(self, base_dn, scope, filter_string, attributes):
        """
        type: filter_string: str
        type: attributes: list(str)
        """
        connection = self.connection
        search_page_size = self.options['search_page_size']

        lc = ldap.controls.libldap.SimplePagedResultsControl(True, size=search_page_size, cookie='')

        msgid = None
        try:
            has_next_page = True
            while has_next_page:
                response_data = None
                result_type = None
                if msgid is not None:
                    result_type, response_data, _rmsgid, serverctrls = connection.result3(msgid)
                    msgid = None
                    pctrls = [c for c in serverctrls
                              if c.controlType == ldap.controls.libldap.SimplePagedResultsControl.controlType]
                    if not pctrls:
                        self.logger.warn('Server ignored RFC 2696 control.')
                        has_next_page = False
                    else:
                        lc.cookie = cookie = pctrls[0].cookie
                        if not cookie:
                            has_next_page = False
                if has_next_page:
                    msgid = connection.search_ext(base_dn, scope,
                                                  filterstr=filter_string, attrlist=attributes, serverctrls=[lc])
                if result_type in self.expected_result_types and (response_data is not None):
                    for item in response_data:
                        yield item
        except GeneratorExit:
            if msgid is not None:
                connection.abandon(msgid)
            raise


class LDAPValueFormatter(object):
    encoding = 'utf-8'

    def __init__(self, string_format):
        """
        :type string_format: unicode
        """
        if string_format is None:
            attribute_names = []
        else:
            formatter = string.Formatter()
            attribute_names = [item[1] for item in formatter.parse(string_format) if item[1]]
        self.string_format = string_format
        self.attribute_names = attribute_names

    def get_attribute_names(self):
        """
        :rtype list(str)
        """
        return self.attribute_names

    def generate_value(self, record):
        """
        :type record: dict
        :rtype (unicode, unicode)
        """
        result = None
        attribute_name = None
        if self.string_format is not None:
            values = {}
            for attribute_name in self.attribute_names:
                value = self.get_attribute_value(record, attribute_name)
                if value is None:
                    values = None
                    break
                values[attribute_name] = value
            if values is not None:
                result = self.string_format.format(**values)
        return result, attribute_name

    @classmethod
    def get_attribute_value(cls, attributes, attribute_name):
        """
        :type attributes: dict
        :type attribute_name: unicode
        """
        if attribute_name in attributes:
            attribute_value = attributes[attribute_name]
            if len(attribute_value) > 0:
                try:
                    return attribute_value[0].decode(cls.encoding)
                except UnicodeError as e:
                    raise AssertionException("Encoding error in value of attribute '%s': %s" % (attribute_name, e))
        return None
