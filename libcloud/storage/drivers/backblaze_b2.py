# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Driver for Backblaze B2 service.
"""

import base64
import hashlib

try:
    import simplejson as json
except ImportError:
    import json

from libcloud.utils.py3 import b
from libcloud.utils.py3 import httplib
from libcloud.utils.py3 import urlparse
from libcloud.utils.files import read_in_chunks
from libcloud.utils.files import exhaust_iterator

from libcloud.common.base import ConnectionUserAndKey
from libcloud.common.base import JsonResponse
from libcloud.common.types import InvalidCredsError
from libcloud.common.types import LibcloudError
from libcloud.storage.providers import Provider
from libcloud.storage.base import Object, Container, StorageDriver

__all__ = [
    'BackblazeB2StorageDriver',

    'BackblazeB2Connection',
    'BackblazeB2AuthConnection'
]

AUTH_API_HOST = 'api.backblaze.com'
API_PATH = '/b2api/v1/'


class BackblazeB2Response(JsonResponse):
    def success(self):
        return self.status in [httplib.OK, httplib.CREATED, httplib.ACCEPTED]

    def parse_error(self):
        status = int(self.status)
        body = self.parse_body()

        if status == httplib.UNAUTHORIZED:
            raise InvalidCredsError(body['message'])

        return self.body


class BackblazeB2AuthConnection(ConnectionUserAndKey):
    host = AUTH_API_HOST
    secure = True
    responseCls = BackblazeB2Response

    def __init__(self, *args, **kwargs):
        super(BackblazeB2AuthConnection, self).__init__(*args, **kwargs)

        # Those attributes are populated after authentication
        self.account_id = None
        self.api_url = None
        self.api_host = None
        self.download_url = None
        self.download_host = None
        self.auth_token = None

    def authenticate(self, force=False):
        """
        :param force: Force authentication if if we have already obtained the
                      token.
        :type force: ``bool``
        """
        if not self._is_authentication_needed(force=force):
            return self

        headers = {}
        action = 'b2_authorize_account'
        auth_b64 = base64.b64encode(b('%s:%s' % (self.user_id, self.key)))
        headers['Authorization'] = 'Basic %s' % (auth_b64.decode('utf-8'))

        action = API_PATH + 'b2_authorize_account'
        resp = self.request(action=action, headers=headers, method='GET')

        if resp.status == httplib.OK:
            self._parse_and_set_auth_info(data=resp.object)
        else:
            raise Exception('Failed to authenticate: %s' % (str(resp.object)))

        return self

    def _parse_and_set_auth_info(self, data):
        result = {}
        self.account_id = data['accountId']
        self.api_url = data['apiUrl']
        self.download_url = data['downloadUrl']
        self.auth_token = data['authorizationToken']

        parsed_api_url = urlparse.urlparse(self.api_url)
        self.api_host = parsed_api_url.netloc

        parsed_download_url = urlparse.urlparse(self.download_url)
        self.download_host = parsed_download_url.netloc

        return result

    def _is_authentication_needed(self, force=False):
        if not self.auth_token or force:
            return True

        return False


class BackblazeB2Connection(ConnectionUserAndKey):
    host = None  # Note: host is set after authentication
    secure = True
    responseCls = BackblazeB2Response

    def __init__(self, *args, **kwargs):
        super(BackblazeB2Connection, self).__init__(*args, **kwargs)

        # Stores info retrieved after authnetication (auth token, api url,
        # dowload url).
        self._auth_conn = BackblazeB2AuthConnection(*args, **kwargs)

    def download_request(self):
        # TODO
        pass

    def upload_request(self):
        # TODO
        pass

    def request(self, action, params=None, data=None, headers=None,
                method='GET', raw=False, include_account_id=False,
                download_request=False, upload_host=None, auth_token=None):
        params = params or {}
        headers = headers or {}

        # Lazily perform authentication
        auth_conn = self._auth_conn.authenticate()

        # Set host
        if raw:
            # TODO: Refactor this mess.
            # File download or upload request:
            if method == 'GET':
                # Download
                self.host = auth_conn.download_host
            elif method == 'POST':
                self.host = upload_host
        else:
            self.host = auth_conn.api_host

        if upload_host:
            self.host = upload_host

        # Provide auth token
        # TODO: Refactor
        if not auth_token:
            auth_token = auth_conn.auth_token

        headers['Authorization'] = '%s' % (auth_token)

        # Include Content-Type
        if not raw and data:
            headers['Content-Type'] = 'application/json'

        # Include account id
        if include_account_id:
            if method == 'GET':
                params['accountId'] = auth_conn.account_id
            elif method == 'POST':
                data = data or {}
                data['accountId'] = auth_conn.account_id

        if not raw and not upload_host:
            action = API_PATH + action
        elif method == 'GET':
            # Download
            action = '/file/' + action

        if data and not upload_host:
            data = json.dumps(data)

        response = super(BackblazeB2Connection, self).request(action=action,
                                                              params=params,
                                                              data=data,
                                                              method=method,
                                                              headers=headers,
                                                              raw=raw)
        return response


class BackblazeB2StorageDriver(StorageDriver):
    connectionCls = BackblazeB2Connection
    name = 'Backblaze B2'
    website = 'https://www.backblaze.com/b2/'
    type = Provider.BACKBLAZE_B2
    hash_type = 'sha1'
    supports_chunked_encoding = False

    def iterate_containers(self):
        resp = self.connection.request(action='b2_list_buckets',
                                       method='GET',
                                       include_account_id=True)
        containers = self._to_containers(data=resp.object)
        return containers

    def iterate_container_objects(self, container):
        # TODO: Support pagination
        params = {'bucketId': container.extra['id']}
        resp = self.connection.request(action='b2_list_file_names',
                                       method='GET',
                                       params=params)
        objects = self._to_objects(data=resp.object, container=container)
        return objects

    def create_container(self, container_name, ex_type='allPrivate'):
        data = {}
        data['bucketName'] = container_name
        data['bucketType'] = ex_type
        resp = self.connection.request(action='b2_create_bucket',
                                       data=data, method='POST',
                                       include_account_id=True)
        container = self._to_container(item=resp.object)
        return container

    def delete_container(self, container):
        data = {}
        data['bucketId'] = container.extra['id']
        resp = self.connection.request(action='b2_delete_bucket',
                                       data=data, method='POST',
                                       include_account_id=True)
        return resp.status == httplib.OK

    def download_object(self, obj, destination_path, overwrite_existing=False,
                        delete_on_failure=True):
        action = self._get_object_download_path(container=obj.container,
                                                obj=obj)
        response = self.connection.request(action=action, method='GET',
                                           raw=True)

        # TODO: Include metadata from response headers
        return self._get_object(obj=obj, callback=self._save_object,
                                response=response,
                                callback_kwargs={
                                    'obj': obj,
                                    'response': response.response,
                                    'destination_path': destination_path,
                                    'overwrite_existing': overwrite_existing,
                                    'delete_on_failure': delete_on_failure
                                },
                                success_status_code=httplib.OK)

    def download_object_as_stream(self, obj, chunk_size=None):
        action = self._get_object_download_path(container=obj.container,
                                                obj=obj)
        response = self.connection.request(action=action, method='GET',
                                           raw=True)

        return self._get_object(obj=obj, callback=read_in_chunks,
                                response=response,
                                callback_kwargs={'iterator': response.response,
                                                 'chunk_size': chunk_size},
                                success_status_code=httplib.OK)

    def upload_object(self, file_path, container, object_name, extra=None,
                      verify_hash=True, headers=None):
        """
        Upload an object.

        Note: This will override file with a same name if it already exists.
        """
        # Note: We don't use any of the base driver functions since Backblaze
        # API requires you to provide SHA1 has upfront and the base methods
        # don't support that
        fh = open(file_path, 'rb')
        iterator = iter(fh)
        iterator = read_in_chunks(iterator=iterator)
        data = exhaust_iterator(iterator=iterator)

        extra = extra or {}
        content_type = extra.get('content_type', 'b2/x-auto')
        meta_data = extra.get('meta_data', {})

        # Note: Backblaze API doesn't support chunked encoding and we need to
        # provide Content-Length up front (this is one inside _upload_object):/
        headers = headers or {}
        headers['X-Bz-File-Name'] = object_name
        headers['Content-Type'] = content_type

        sha1 = hashlib.sha1()
        sha1.update(data.encode())
        headers['X-Bz-Content-Sha1'] = sha1.hexdigest()

        # Include optional meta-data (up to 10 items)
        for key, value in meta_data:
            # TODO: Encode / escape key
            headers['X-Bz-Info-%s' % (key)] = value

        upload_data = self.ex_get_upload_data(container_id=container.extra['id'])
        upload_token = upload_data['authorizationToken']
        parsed_url = urlparse.urlparse(upload_data['uploadUrl'])

        upload_host = parsed_url.netloc
        request_path = parsed_url.path

        response = self.connection.request(action=request_path, method='POST',
                                           headers=headers,
                                           upload_host=upload_host,
                                           auth_token=upload_token,
                                           data=data)

        if response.status == httplib.OK:
            obj = self._to_object(item=response.object, container=container)
            return obj
        else:
            body = response.read()
            raise LibcloudError('Upload failed. status_code=%s, body=%s' %
                                (response.status, body), driver=self)

    def delete_object(self, obj):
        data = {}
        data['fileName'] = obj.name
        data['fileId'] = obj.extra['fileId']
        resp = self.connection.request(action='b2_delete_file_version',
                                       data=data, method='POST')
        return resp.status == httplib.OK

    def ex_get_object(self, object_id):
        params = {}
        params['fileId'] = object_id
        resp = self.connection.request(action='b2_get_file_info',
                                       method='GET',
                                       params=params)
        obj = self._to_object(item=resp.object, container=None)
        return obj

    def ex_hide_object(self, container_id, object_name):
        data = {}
        data['bucketId'] = container_id
        data['fileName'] = object_name
        resp = self.connection.request(action='b2_hide_file',
                                       data=data, method='POST')
        obj = self._to_object(item=resp.object, container=None)
        return obj

    def ex_list_object_versions(self, container_id, ex_start_file_name=None,
                                ex_start_file_id=None, ex_max_file_count=None):
        params = {}
        params['bucketId'] = container_id

        if ex_start_file_name:
            params['startFileName'] = ex_start_file_name

        if ex_start_file_id:
            params['startFileId'] = ex_start_file_id

        if ex_max_file_count:
            params['maxFileCount'] = ex_max_file_count

        resp = self.connection.request(action='b2_list_file_versions',
                                       params=params, method='GET')
        objects = self._to_objects(data=resp.object, container=None)
        return objects

    def ex_get_upload_data(self, container_id):
        """
        Retrieve information used for uploading files (upload url, auth token,
        etc).

        :rype: ``dict``
        """
        # TODO: This is static (AFAIK) so it could be cached
        params = {}
        params['bucketId'] = container_id
        response = self.connection.request(action='b2_get_upload_url',
                                           method='GET',
                                           params=params)
        return response.object

    def ex_get_upload_url(self, container_id):
        """
        Retrieve URL used for file uploads.

        :rtype: ``str``
        """
        result = self.ex_get_upload_data(container_id=container_id)
        upload_url = result['uploadUrl']
        return upload_url

    def _to_containers(self, data):
        result = []
        for item in data['buckets']:
            container = self._to_container(item=item)
            result.append(container)

        return result

    def _to_container(self, item):
        extra = {}
        extra['id'] = item['bucketId']
        extra['bucketType'] = item['bucketType']
        container = Container(name=item['bucketName'], extra=extra,
                              driver=self)
        return container

    def _to_objects(self, data, container):
        result = []
        for item in data['files']:
            obj = self._to_object(item=item, container=container)
            result.append(obj)

        return result

    def _to_object(self, item, container=None):
        extra = {}
        extra['fileId'] = item['fileId']
        extra['uploadTimestamp'] = item.get('uploadTimestamp', None)
        size = item.get('size', item.get('contentLength', None))
        hash = item.get('contentSha1', None)
        meta_data = item.get('fileInfo', {})
        obj = Object(name=item['fileName'], size=size, hash=hash, extra=extra,
                     meta_data=meta_data, container=container, driver=self)
        return obj

    def _get_object_download_path(self, container, obj):
        """
        Return a path used in the download requests.

        :rtype: ``str``
        """
        path = container.name + '/' + obj.name
        return path
