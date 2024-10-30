"""TickTick Mod Integration"""

import re
import pytz
import json
import random
import logging
import secrets
import requests
import datetime

from functools import wraps
from calendar import monthrange

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

# from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.const import Platform
from .const import DOMAIN, CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_ACCESS_TOKEN
from .coordinator import TickTickDataUpdateCoordinator

PLATFORMS: list[Platform] = [Platform.TODO]

DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
ALID_HEX_VALUES = "^#([A-Fa-f0-9]{6}|[A-Fa-f0-9]{3})$"

_LOGGER = logging.getLogger(__name__)

def requests_retry_session(retries=3, backoff_factor=1, status_forcelist=(405, 500, 502, 504), session=None, allowed_methods=frozenset(['GET', 'POST', 'PUT', 'DELETE'])):
    session = session or requests.session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=allowed_methods
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


class OAuth2:
    OAUTH_AUTHORIZE_URL = "https://ticktick.com/oauth/authorize"
    OBTAIN_TOKEN_URL = "https://ticktick.com/oauth/token"
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, access_token: str, scope: str = "tasks:write tasks:read", state: str = None, session=None):
        # If a proper session is passed then we will just use the existing session
        self.session = session or requests_retry_session()
        # Set the client_id
        self._client_id = client_id
        # Set the client_secret
        self._client_secret = client_secret
        # Set the redirect_uri
        self._redirect_uri = redirect_uri
        # Set the scope
        self._scope = scope
        # Set the state
        self._state = state
        # Initialize code parameter
        self._code = None
        # Set the access token
        self.access_token_info = json.loads(access_token)

def convert_local_time_to_utc(original_time, time_zone: str):
    utc = pytz.utc
    time_zone = pytz.timezone(time_zone)
    original_time = original_time.strftime(DATE_FORMAT)
    time_object = datetime.datetime.strptime(original_time, DATE_FORMAT)
    time_zone_dt = time_zone.localize(time_object)
    return time_zone_dt.astimezone(utc).replace(tzinfo=None)


def convert_date_to_tick_tick_format(datetime_obj, tz: str):
    date = convert_local_time_to_utc(datetime_obj, tz)
    date = date.replace(tzinfo=datetime.timezone.utc).isoformat()
    date = date[::-1].replace(":", "", 1)[::-1]
    return date

class TaskManager:
    TASK_CREATE_ENDPOINT = "/open/v1/task"
    def __init__(self, client_class):
        self._client = client_class
        self.oauth_access_token = ''
        if self._client.oauth_manager.access_token_info is not None:
            self.oauth_access_token = self._client.oauth_manager.access_token_info['access_token']
        self.oauth_headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer {}'.format(self.oauth_access_token), 'User-Agent': self._client.USER_AGENT}
        self.headers = self._client.HEADERS
    def _generate_create_url(self):
        CREATE_ENDPOINT = "/open/v1/task"
        return self._client.OPEN_API_BASE_URL + CREATE_ENDPOINT
    def create(self, task):
        url = self._generate_create_url()
        response = self._client.http_post(url=url, json=task, headers=self.oauth_headers)
        self._client.sync()
        if response['projectId'] == 'inbox':
            response['projectId'] = self._client.inbox_id
        return response
    def _generate_update_url(self, taskID: str):
        UPDATE_ENDPOINT = f"/open/v1/task/{taskID}"
        return self._client.OPEN_API_BASE_URL + UPDATE_ENDPOINT
    def update(self, task):
        url = self._generate_update_url(task['id'])
        response = self._client.http_post(url=url, json=task, headers=self.oauth_headers)
        self._client.sync()
        return response
    def _generate_mark_complete_url(self, projectID, taskID):
        COMPLETE_ENDPOINT = f"/open/v1/project/{projectID}/task/{taskID}/complete"
        return self._client.OPEN_API_BASE_URL + COMPLETE_ENDPOINT
    def complete(self, task: dict):
        url = self._generate_mark_complete_url(task['projectId'], task['id'])
        response = self._client.http_post(url=url, json=task, headers=self.oauth_headers)
        self._client.sync()
        if response == '':
            return task
        return response
    def _generate_delete_url(self):
        return self._client.BASE_URL + 'batch/task'
    def delete(self, task):
        url = self._generate_delete_url()
        to_delete = []
        if isinstance(task, dict):
            if task['projectId'] == 'inbox':
                task['projectId'] = self._client.inbox_id
            delete_dict = {'projectId': task['projectId'], 'taskId': task['id']}
            to_delete.append(delete_dict)
        else:
            for item in task:
                if item['projectId'] == 'inbox':
                    item['projectId'] = self._client.inbox_id
                delete_dict = {'projectId': item['projectId'], 'taskId': item['id']}
                to_delete.append(delete_dict)
        payload = {'delete': to_delete}
        self._client.http_post(url, json=payload, cookies=self._client.cookies, headers=self.headers)
        self._client.sync()
        return task
    def make_subtask(self, obj, parent: str):
        if not isinstance(obj, dict) and not isinstance(obj, list):
            raise TypeError('obj must be a dictionary or list of dictionaries')
        if not isinstance(parent, str):
            raise TypeError('parent must be a string')
        if isinstance(obj, dict):
            obj = [obj]
        parent_obj = self._client.get_by_id(search='tasks', obj_id=parent)
        if not parent_obj:
            raise ValueError("Parent task must exist before creating sub-tasks")
        ids = []
        for o in obj:
            if o['projectId'] != parent_obj['projectId']:
                raise ValueError("All tasks must be in the same project as the parent")
            ids.append(o['id'])
        subtasks = []
        for i in ids:  # Create the object dictionaries for setting the subtask
            temp = {
                'parentId': parent,
                'projectId': parent_obj['projectId'],
                'taskId': i
            }
            subtasks.append(temp)
        url = self._client.BASE_URL + 'batch/taskParent'
        response = self._client.http_post(url, json=subtasks, cookies=self._client.cookies, headers=self.headers)
        self._client.sync()
        subtasks = []
        for task_id in ids:
            subtasks.append(self._client.get_by_id(task_id, search='tasks'))
        if len(subtasks) == 1:
            return subtasks[0]  # Return just the dictionary object if its a single task
        else:
            return subtasks
    def move(self, obj, new: str):
        if not isinstance(obj, dict) and not isinstance(obj, list):
            raise TypeError('obj should be a dict or list of dicts')
        if not isinstance(new, str):
            raise TypeError('new should be a string')
        if new != self._client.inbox_id:
            project = self._client.get_by_id(new, search='projects')
            if not project:
                raise ValueError('The ID for the new project does not exist')
        if isinstance(obj, dict):
            obj = [obj]
        move_tasks = []
        project_id = obj[0]['projectId']
        for task in obj:
            if task['projectId'] != project_id:
                raise ValueError('All the tasks must come from the same project')
            else:
                move_tasks.append({
                    'fromProjectId': project_id,
                    'taskId': task['id'],
                    'toProjectId': new
                })
        url = self._client.BASE_URL + 'batch/taskProject'
        self._client.http_post(url, json=move_tasks, cookies=self._client.cookies, headers=self.headers)
        self._client.sync()
        # Return the tasks in the new list
        ids = [x['id'] for x in obj]
        return_list = []
        for i in ids:
            return_list.append(self._client.get_by_id(i))
        if len(return_list) == 1:
            return return_list[0]
        else:
            return return_list
    def move_all(self, old: str, new: str) -> list:
        if old != self._client.inbox_id:
            old_list = self._client.get_by_fields(id=old, search='projects')
            if not old_list:
                raise ValueError(f"Project Id '{old}' Does Not Exist")
        if new != self._client.inbox_id:
            new_list = self._client.get_by_fields(id=new, search='projects')
            if not new_list:
                raise ValueError(f"Project Id '{new}' Does Not Exist")
        tasks = self.get_from_project(old)
        if not tasks:
            return tasks  # No tasks to move so just return the empty list
        task_project = []  # List containing all the tasks that will be updated
        for task in tasks:
            task_project.append({
                'fromProjectId': old,
                'taskId': task['id'],
                'toProjectId': new
            })
        url = self._client.BASE_URL + 'batch/taskProject'
        self._client.http_post(url, json=task_project, cookies=self._client.cookies, headers=self.headers)
        self._client.sync()
        return self._client.task.get_from_project(new)
    def get_from_project(self, project: str):
        if project != self._client.inbox_id:
            obj = self._client.get_by_fields(id=project, search='projects')
            if not obj:
                raise ValueError(f"List Id '{project}' Does Not Exist")
        tasks = self._client.get_by_fields(projectId=project, search='tasks')
        if isinstance(tasks, dict):
            return [tasks]
        else:
            return tasks
    def get_completed(self, start, end=None, full: bool = True, tz: str = None) -> list:
        url = self._client.BASE_URL + 'project/all/completed'
        if tz is None:
            tz = self._client.time_zone
        if not isinstance(start, datetime.datetime):
            raise TypeError('Start Must Be A Datetime Object')
        if not isinstance(end, datetime.datetime) and end is not None:
            raise TypeError('End Must Be A Datetime Object')
        if end is not None and start > end:
            raise ValueError('Invalid Date Range: Start Date Occurs After End Date')
        if tz not in pytz.all_timezones_set:
            raise KeyError('Invalid Time Zone')
        if end is None:
            start = datetime.datetime(start.year, start.month, start.day, 0, 0, 0)
            end = datetime.datetime(start.year, start.month, start.day, 23, 59, 59)
        elif full is True and end is not None:
            start = datetime.datetime(start.year, start.month, start.day, 0, 0, 0)
            end = datetime.datetime(end.year, end.month, end.day, 23, 59, 59)
        start = convert_local_time_to_utc(start, tz)
        end = convert_local_time_to_utc(end, tz)
        parameters = {
            'from': start.strftime(DATE_FORMAT),
            'to': end.strftime(DATE_FORMAT),
            'limit': 100
        }
        response = self._client.http_get(url, params=parameters, cookies=self._client.cookies, headers=self.headers)
        return response
    def dates(self, start, due=None, tz=None):
        dates = {}
        if tz is not None:
            dates['timeZone'] = tz
        else:
            tz = self._client.time_zone
        if due is None:
            if start.hour != 0 or start.minute != 0 or start.second != 0 or start.microsecond != 0:
                dates['startDate'] = convert_date_to_tick_tick_format(start, tz)
                dates['allDay'] = False
            else:
                dates['startDate'] = convert_date_to_tick_tick_format(start, tz)
                dates['allDay'] = True
            return dates
        if (start.hour != 0 or start.minute != 0 or start.second != 0 or start.microsecond != 0
                or due.hour != 0 or due.minute != 0 or due.second != 0 or due.microsecond != 0):
            dates['startDate'] = convert_date_to_tick_tick_format(start, tz)
            dates['dueDate'] = convert_date_to_tick_tick_format(due, tz)
            dates['allDay'] = False
            return dates
        days = monthrange(due.year, due.month)
        if due.day + 1 > days[1]:  # Last day of the month
            if due.month + 1 > 12:  # Last month of the year
                year = due.year + 1  # Both last day of month and last day of year
                day = 1
                month = 1
            else:  # Not last month of year, just reset the day and increment the month
                year = due.year
                month = due.month + 1
                day = 1
        else:  # Dont have to worry about incrementing year or month
            year = due.year
            day = due.day + 1
            month = due.month
        due = datetime.datetime(year, month, day)  # No hours, mins, or seconds needed
        dates['startDate'] = convert_date_to_tick_tick_format(start, tz)
        dates['dueDate'] = convert_date_to_tick_tick_format(due, tz)
        dates['allDay'] = True
        return dates
    def builder(self,
                title: str = '',
                projectId: str = None,
                content: str = None,
                desc: str = None,
                allDay: bool = None,
                startDate: datetime.datetime = None,
                dueDate: datetime.datetime = None,
                timeZone: str = None,
                reminders: list = None,
                repeat: str = None,
                priority: int = None,
                sortOrder: int = None,
                items: list = None):
        task = {'title': title}
        if projectId is not None:
            task['projectId'] = projectId
        if content is not None:
            task['content'] = content
        if desc is not None:
            task['desc'] = desc
        if allDay is not None:
            task['allDay'] = allDay
        if reminders is not None:
            task['reminders'] = reminders
        if repeat is not None:
            task['repeat'] = repeat
        if priority is not None:
            task['priority'] = priority
        if sortOrder is not None:
            task['sortOrder'] = sortOrder
        if items is not None:
            task['items'] = items
        dates = {}
        # date conversions
        if startDate is not None:
            dates = self.dates(startDate, dueDate, timeZone)
        # merge dicts
        return {**dates, **task}


def _sort_string_value(sort_type: int) -> str:
    if sort_type not in {0, 1, 2, 3}:
        raise ValueError(f"Sort Number '{sort_type}' Is Invalid -> Must Be 0, 1, 2 or 3")
    else:
        sort_dict = {0: 'project', 1: 'dueDate', 2: 'title', 3: 'priority'}
    return sort_dict[sort_type]


class TagsManager:
    SORT_DICTIONARY = {0: 'project', 1: 'dueDate', 2: 'title', 3: 'priority'}
    def __init__(self, client_class):
        self._client = client_class
        self.access_token = self._client.access_token
        self.headers = self._client.HEADERS
    def _sort_string_value(self, sort_type: int) -> str:
        if sort_type not in {0, 1, 2, 3}:
            raise ValueError(f"Sort Number '{sort_type}' Is Invalid -> Must Be 0, 1, 2 or 3")
        return self.SORT_DICTIONARY[sort_type]
    def _check_fields(self, label: str = None, color: str = 'random', parent_label: str = None, sort: int = None) -> dict:
        if label is not None:
            if not isinstance(label, str):
                raise TypeError(f"Label Must Be A String")
            tag_list = self._client.get_by_fields(search='tags', name=label.lower())  # Name is lowercase version of label
            if tag_list:
                raise ValueError(f"Invalid Tag Name '{label}' -> It Already Exists")
        if not isinstance(color, str):
            raise TypeError(f"Color Must Be A Hex Color String")
        if color.lower() == 'random':
            color = generate_hex_color()  # Random color will be generated
        elif color is not None:
            if not check_hex_color(color):
                raise ValueError('Invalid Hex Color String')
        if parent_label is not None:
            if not isinstance(parent_label, str):
                raise TypeError(f"Parent Name Must Be A String")
            parent_label = parent_label.lower()
            parent = self._client.get_by_fields(search='tags', name=parent_label)
            if not parent:
                raise ValueError(f"Invalid Parent Name '{parent_label}' -> Does Not Exist")
        if sort is None:
            sort = 'project'
        else:
            sort = _sort_string_value(sort)
        return {'label': label, 'color': color, 'parent': parent_label, 'sortType': sort, 'name': label.lower()}
    def builder(self,
                label: str,
                color: str = 'random',
                parent: str = None,
                sort: int = None
                ) -> dict:
        return self._check_fields(label, color=color, parent_label=parent, sort=sort)
    def create(self, label, color: str = 'random', parent: str = None, sort: int = None):
        batch = False  # Bool signifying batch create or not
        if isinstance(label, list):
            obj = label  # Assuming all correct objects
            batch = True
        else:
            if not isinstance(label, str):
                raise TypeError('Required Positional Argument Must Be A String or List of Tag Objects')
            obj = self.builder(label=label, color=color, parent=parent, sort=sort)
        if not batch:
            obj = [obj]
        url = self._client.BASE_URL + 'batch/tag'
        payload = {'add': obj}
        response = self._client.http_post(url, json=payload, cookies=self._client.cookies, headers=self.headers)
        self._client.sync()
        if not batch:
            return self._client.get_by_etag(self._client.parse_etag(response), search='tags')
        else:
            etag = response['id2etag']
            etag2 = list(etag.keys())  # Tag names are out of order
            labels = [x['name'] for x in obj]  # Tag names are in order
            items = [''] * len(obj)  # Create enough spots for the objects
            for tag in etag2:
                index = labels.index(tag)  # Object of the index is here
                actual_etag = etag[tag]  # Get the actual etag
                found = self._client.get_by_etag(actual_etag, search='tags')
                items[index] = found  # Place at the correct index
            if len(items) == 1:
                return items[0]
            else:
                return items
    def rename(self, old: str, new: str) -> dict:
        if not isinstance(old, str) or not isinstance(new, str):
            raise TypeError('Old and New Must Be Strings')
        old = old.lower()
        obj = self._client.get_by_fields(name=old, search='tags')
        if not obj:
            raise ValueError(f"Tag '{old}' Does Not Exist To Rename")
        temp_new = new.lower()
        found = self._client.get_by_fields(name=temp_new, search='tags')
        if found:
            raise ValueError(f"Name '{new}' Already Exists -> Cannot Duplicate Name")
        url = self._client.BASE_URL + 'tag/rename'
        payload = {
            'name': obj['name'],
            'newName': new
        }
        response = self._client.http_put(url, json=payload, cookies=self._client.cookies, headers=self.headers)
        self._client.sync()
        new_obj = self._client.get_by_fields(name=temp_new, search='tags')
        return self._client.get_by_etag(new_obj['etag'], search='tags')
    def color(self, label: str, color: str) -> dict:
        if not isinstance(label, str) or not isinstance(color, str):
            raise TypeError('Label and Color Must Be Strings')
        label = label.lower()
        obj = self._client.get_by_fields(name=label, search='tags')
        if not obj:
            raise ValueError(f"Tag '{label}' Does Not Exist To Update")
        if not check_hex_color(color):
            raise ValueError(f"Hex Color String '{color}' Is Not Valid")
        obj['color'] = color  # Set the color
        url = self._client.BASE_URL + 'batch/tag'
        payload = {
            'update': [obj]
        }
        response = self._client.http_post(url, json=payload, cookies=self._client.cookies, headers=self.headers)
        self._client.sync()
        return self._client.get_by_etag(response['id2etag'][obj['name']])
    def sorting(self, label: str, sort: int) -> dict:
        if not isinstance(label, str) or not isinstance(sort, int):
            raise TypeError('Label Must Be A String and Sort Must Be An Int')
        label = label.lower()
        obj = self._client.get_by_fields(name=label, search='tags')
        if not obj:
            raise ValueError(f"Tag '{label}' Does Not Exist To Update")
        sort = self._sort_string_value(sort)  # Get the sort string for the value
        obj['sortType'] = sort  # set the object field
        url = self._client.BASE_URL + 'batch/tag'
        payload = {
            'update': [obj]
        }
        response = self._client.http_post(url, json=payload, cookies=self._client.cookies, headers=self.headers)
        self._client.sync()
        return self._client.get_by_etag(response['id2etag'][obj['name']])
    def nesting(self, child: str, parent: str) -> dict:
        if not isinstance(child, str):
            raise TypeError('Inputs Must Be Strings')
        if parent is not None:
            if not isinstance(parent, str):
                raise TypeError('Inputs Must Be Strings')
        child = child.lower()
        obj = self._client.get_by_fields(name=child, search='tags')
        if not obj:
            raise ValueError(f"Tag '{child}' Does Not Exist To Update")
        try:
            if obj['parent']:
                if parent is not None:  # Case 3
                    if obj['parent'] == parent.lower():
                        return obj
                    else:
                        new_p = parent.lower()
                        obj['parent'] = new_p
                else:
                    new_p = obj['parent']  # Case 4
                    obj['parent'] = ''
            elif obj['parent'] is None:
                raise ValueError('Parent Does Not Exist')
        except KeyError:
            if parent is not None:  # Wants a different parent
                new_p = parent.lower()  # -> Case 1
                obj['parent'] = new_p
            else:  # Doesn't want a parent -> Case 2
                return obj  # We don't have to do anything if no parent and doesn't want a parent
        pobj = self._client.get_by_fields(name=new_p, search='tags')
        if not pobj:
            raise ValueError(f"Tag '{parent}' Does Not Exist To Set As Parent")
        url = self._client.BASE_URL + 'batch/tag'
        payload = {
            'update': [pobj, obj]
        }
        response = self._client.http_post(url, json=payload, cookies=self._client.cookies, headers=self.headers)
        self._client.sync()
        return self._client.get_by_etag(response['id2etag'][obj['name']], search='tags')
    def update(self, obj):
        batch = False  # Bool signifying batch create or not
        if isinstance(obj, list):
            # Batch tag creation triggered
            obj_list = obj  # Assuming all correct objects
            batch = True
        else:
            if not isinstance(obj, dict):
                raise TypeError('Required Positional Argument Must Be A Dict or List of Tag Objects')
        if not batch:
            obj_list = [obj]
        url = self._client.BASE_URL + 'batch/tag'
        payload = {'update': obj_list}
        response = self._client.http_post(url, json=payload, cookies=self._client.cookies, headers=self.headers)
        self._client.sync()
        if not batch:
            return self._client.get_by_etag(self._client.parse_etag(response), search='tags')
        else:
            etag = response['id2etag']
            etag2 = list(etag.keys())  # Tag names are out of order
            labels = [x['name'] for x in obj_list]  # Tag names are in order
            items = [''] * len(obj_list)  # Create enough spots for the objects
            for tag in etag2:
                index = labels.index(tag)  # Object of the index is here
                actual_etag = etag[tag]  # Get the actual etag
                found = self._client.get_by_etag(actual_etag, search='tags')
                items[index] = found  # Place at the correct index
            return items
    def merge(self, label, merged: str):
        if not isinstance(merged, str):
            raise ValueError('Merged Must Be A String')
        if not isinstance(label, str) and not isinstance(label, list):
            raise ValueError(f"Label must be a string or a list.")
        merged = merged.lower()
        kept_obj = self._client.get_by_fields(name=merged, search='tags')
        if not kept_obj:
            raise ValueError(f"Kept Tag '{merged}' Does Not Exist To Merge")
        merge_queue = []
        if isinstance(label, str):
            string = label.lower()
            retrieved = self._client.get_by_fields(name=string, search='tags')
            if not retrieved:
                raise ValueError(f"Tag '{label}' Does Not Exist To Merge")
            merge_queue.append(retrieved)
        else:
            for item in label:  # Loop through the items in the list and check items are a string and exist
                if not isinstance(item, str):
                    raise ValueError(f"Item '{item}' Must Be A String")
                string = item.lower()
                found = self._client.get_by_fields(name=string, search='tags')
                if not found:
                    raise ValueError(f"Tag '{item}' Does Not Exist To Merge")
                merge_queue.append(found)
        for labels in merge_queue:
            url = self._client.BASE_URL + 'tag/merge'
            payload = {
                'name': labels['name'],
                'newName': kept_obj['name']
            }
            self._client.http_put(url, json=payload, cookies=self._client.cookies, headers=self.headers)
        self._client.sync()
        return kept_obj
    def delete(self, label):
        if not isinstance(label, str) and not isinstance(label, list):
            raise TypeError('Label Must Be A String or List Of Strings')
        url = self._client.BASE_URL + 'tag'
        if isinstance(label, str):
            label = [label]  # If a singular string we are going to add it to a list
        objects = []
        for lbl in label:
            if not isinstance(lbl, str):
                raise TypeError(f"'{lbl}' Must Be A String")
            lbl = lbl.lower()
            tag_obj = self._client.get_by_fields(name=lbl, search='tags')  # Get the tag object
            if not tag_obj:
                raise ValueError(f"Tag '{lbl}' Does Not Exist To Delete")
            params = {
                'name': tag_obj['name']
            }
            response = self._client.http_delete(url, params=params, cookies=self._client.cookies, headers=self.headers)
            objects.append(self._client.delete_from_local_state(search='tags', etag=tag_obj['etag']))
        self._client.sync()
        if len(objects) == 1:
            return objects[0]
        else:
            return objects

class SettingsManager:
    def __init__(self, client_class):
        self._client = client_class
        self.access_token = ''
    def get_templates(self):
        # https://api.ticktick.com/api/v2/templates
        pass
    def get_user_settings(self):
        # https://api.ticktick.com/api/v2/user/preferences/settings?includeWeb=true
        pass

def logged_in(func):
    @wraps(func)
    def call(self, *args, **kwargs):
        if not self.oauth_access_token:
            raise RuntimeError('ERROR -> Not Logged In')
        return func(self, *args, **kwargs)
    return call

def generate_hex_color() -> str:
    num = random.randint(1118481, 16777215)
    hex_num = format(num, 'x')
    return '#' + hex_num

def check_hex_color(color: str) -> bool:
    check_color = re.search(VALID_HEX_VALUES, color)
    if not check_color:
        return False
    else:
        return True

class ProjectManager:
    def __init__(self, client_class):
        self._client = client_class
        self.access_token = self._client.access_token
        self.headers = self._client.HEADERS
    def builder(self, name: str, color: str = 'random', project_type: str = 'TASK', folder_id: str = None) -> dict:
        if not isinstance(name, str):
            raise TypeError("Name must be a string")
        if not isinstance(color, str) and color is not None:
            raise TypeError("Color must be a string")
        if not isinstance(project_type, str):
            raise TypeError("Project type must be a string")
        if not isinstance(folder_id, str) and folder_id is not None:
            raise TypeError("Folder id must be a string")
        id_list = self._client.get_by_fields(search='projects', name=name)
        if id_list:
            raise ValueError(f"Invalid Project Name '{name}' -> It Already Exists")
        if folder_id is not None:
            parent = self._client.get_by_id(folder_id, search='project_folders')
            if not parent:
                raise ValueError(f"Parent Id {folder_id} Does Not Exist")
        if project_type != 'TASK' and project_type != 'NOTE':
            raise ValueError(f"Invalid Project Type '{project_type}' -> Should be 'TASK' or 'NOTE'")
        if color == 'random':
            color = generate_hex_color()  # Random color will be generated
        elif color is not None:
            if not check_hex_color(color):
                raise ValueError('Invalid Hex Color String')
        return {'name': name, 'color': color, 'kind': project_type, 'groupId': folder_id}
    def create(self, name, color: str = 'random', project_type: str = 'TASK', folder_id: str = None):
        if isinstance(name, list):
            obj = name
            batch = True
        elif isinstance(name, str):
            batch = False
            obj = self.builder(name=name, color=color, project_type=project_type, folder_id=folder_id)
            obj = [obj]
        else:
            raise TypeError(f"Required Positional Argument Must Be A String or List of Project Objects")
        url = self._client.BASE_URL + 'batch/project'
        payload = {'add': obj}
        response = self._client.http_post(url, json=payload, cookies=self._client.cookies, headers=self.headers)
        self._client.sync()
        if len(obj) == 1:
            return self._client.get_by_id(self._client.parse_id(response), search='projects')
        else:
            etag = response['id2etag']
            etag2 = list(etag.keys())  # Get the ids
            items = [''] * len(obj)  # Create enough spots for the objects
            for proj_id in etag2:
                found = self._client.get_by_id(proj_id, search='projects')
                for original in obj:
                    if found['name'] == original['name']:
                        # Get the index of original
                        index = obj.index(original)
                        # Place found at the index in return list
                        items[index] = found
            return items
    def update(self, obj):
        if not isinstance(obj, dict) and not isinstance(obj, list):
            raise TypeError("Project objects must be a dict or list of dicts.")
        if isinstance(obj, dict):
            tasks = [obj]
        else:
            tasks = obj
        url = self._client.BASE_URL + 'batch/project'
        payload = {'update': tasks}
        response = self._client.http_post(url, json=payload, cookies=self._client.cookies, headers=self.headers)
        self._client.sync()
        if len(tasks) == 1:
            return self._client.get_by_id(self._client.parse_id(response), search='projects')
        else:
            etag = response['id2etag']
            etag2 = list(etag.keys())  # Get the ids
            items = [''] * len(obj)  # Create enough spots for the objects
            for proj_id in etag2:
                found = self._client.get_by_id(proj_id, search='projects')
                for original in obj:
                    if found['name'] == original['name']:
                        index = obj.index(original)
                        items[index] = found
            return items
    def delete(self, ids):
        if not isinstance(ids, str) and not isinstance(ids, list):
            raise TypeError('Ids Must Be A String or List Of Strings')
        if isinstance(ids, str):
            proj = self._client.get_by_fields(id=ids, search='projects')
            if not proj:
                raise ValueError(f"Project '{ids}' Does Not Exist To Delete")
            ids = [ids]
        else:
            for i in ids:
                proj = self._client.get_by_fields(id=i, search='projects')
                if not proj:
                    raise ValueError(f"Project '{i}' Does Not Exist To Delete")
        url = self._client.BASE_URL + 'batch/project'
        payload = {
            'delete': ids
        }
        self._client.http_post(url, json=payload, cookies=self._client.cookies, headers=self.headers)
        deleted_list = []
        for current_id in ids:
            tasks = self._client.task.get_from_project(current_id)
            for task in tasks:
                self._client.delete_from_local_state(id=task['id'], search='tasks')
            deleted_list.append(self._client.delete_from_local_state(id=current_id, search='projects'))
        if len(deleted_list) == 1:
            return deleted_list[0]
        else:
            return deleted_list
    def archive(self, ids):
        if not isinstance(ids, str) and not isinstance(ids, list):
            raise TypeError('Ids Must Be A String or List Of Strings')
        objs = []
        if isinstance(ids, str):
            proj = self._client.get_by_fields(id=ids, search='projects')
            if not proj:
                raise ValueError(f"Project '{ids}' Does Not Exist To Archive")
            proj['closed'] = True
            objs = [proj]
        else:
            for i in ids:
                proj = self._client.get_by_fields(id=i, search='projects')
                if not proj:
                    raise ValueError(f"Project '{i}' Does Not Exist To Archive")
                proj['closed'] = True
                objs.append(proj)
        return self.update(objs)
    def create_folder(self, name):
        if not isinstance(name, str) and not isinstance(name, list):
            raise TypeError('Name Must Be A String or List Of Strings')
        objs = []
        if isinstance(name, str):
            names = {'name': name,'listType': 'group'}
            objs = [names]
        else:
            for nm in name:
                objs.append({'name': nm,'listType': 'group'})
        url = self._client.BASE_URL + 'batch/projectGroup'
        payload = {'add': objs}
        response = self._client.http_post(url, json=payload, cookies=self._client.cookies, headers=self.headers)
        self._client.sync()
        if len(objs) == 1:
            return self._client.get_by_id(self._client.parse_id(response), search='project_folders')
        else:
            etag = response['id2etag']
            etag2 = list(etag.keys())  # Get the ids
            items = [''] * len(objs)  # Create enough spots for the objects
            for proj_id in etag2:
                found = self._client.get_by_id(proj_id, search='project_folders')
                for original in objs:
                    if found['name'] == original['name']:
                        index = objs.index(original)
                        items[index] = found
            return items
    def update_folder(self, obj):
        if not isinstance(obj, dict) and not isinstance(obj, list):
            raise TypeError("Project objects must be a dict or list of dicts.")
        if isinstance(obj, dict):
            tasks = [obj]
        else:
            tasks = obj
        url = self._client.BASE_URL + 'batch/projectGroup'
        payload = {'update': tasks}
        response = self._client.http_post(url, json=payload, cookies=self._client.cookies, headers=self.headers)
        self._client.sync()
        if len(tasks) == 1:
            return self._client.get_by_id(self._client.parse_id(response), search='project_folders')
        else:
            etag = response['id2etag']
            etag2 = list(etag.keys())  # Get the ids
            items = [''] * len(tasks)  # Create enough spots for the objects
            for proj_id in etag2:
                found = self._client.get_by_id(proj_id, search='project_folders')
                for original in tasks:
                    if found['name'] == original['name']:
                        index = tasks.index(original)
                        items[index] = found
            return items
    def delete_folder(self, ids):
        if not isinstance(ids, str) and not isinstance(ids, list):
            raise TypeError('Ids Must Be A String or List Of Strings')
        if isinstance(ids, str):
            proj = self._client.get_by_fields(id=ids, search='project_folders')
            if not proj:
                raise ValueError(f"Project Folder '{ids}' Does Not Exist To Delete")
            ids = [ids]
        else:
            for i in ids:
                proj = self._client.get_by_fields(id=i, search='project_folders')
                if not proj:
                    raise ValueError(f"Project Folder '{i}' Does Not Exist To Delete")
        url = self._client.BASE_URL + 'batch/projectGroup'
        payload = {'delete': ids}
        self._client.http_post(url, json=payload, cookies=self._client.cookies, headers=self.headers)
        deleted_list = []
        for current_id in ids:
            deleted_list.append(self._client.get_by_id(current_id, search='project_folders'))
        self._client.sync()
        if len(deleted_list) == 1:
            return deleted_list[0]
        else:
            return deleted_list

class FocusTimeManager:
    def __init__(self, client_class):
        self._client = client_class
        self.access_token = ''
    def start(self):
        pass

class HabitManager:
    def __init__(self, client_class):
        self._client = client_class
        self.access_token = ''
    def create(self):
        pass
    def update(self):
        pass

class PomoManager:
    def __init__(self, client_class):
        self._client = client_class
        self.access_token = ''
    def start(self):
        pass
    def statistics(self):
        # https://api.ticktick.com/api/v2/statistics/general
        pass

class TickTickClient:
    BASE_URL = 'https://api.ticktick.com/api/v2/'
    OPEN_API_BASE_URL = 'https://api.ticktick.com'
    INITIAL_BATCH_URL = BASE_URL + 'batch/check/0'
    USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0"
    X_DEVICE_ = '{"platform":"web","os":"OS X","device":"Firefox 123.0","name":"unofficial api!","version":4531,' \
                '"id":"6490' + secrets.token_hex(10) + '","channel":"website","campaign":"","websocket":""}'
    HEADERS = {'User-Agent': USER_AGENT,'x-device': X_DEVICE_}
    def __init__(self, username: str, password: str, oauth: OAuth2) -> None:
        self.access_token = None
        self.cookies = {}
        self.time_zone = ''
        self.profile_id = ''
        self.inbox_id = ''
        self.state = {}
        self.reset_local_state()
        self.oauth_manager = oauth
        self._session = self.oauth_manager.session
        self._prepare_session(username, password)
        self.focus = FocusTimeManager(self)
        self.habit = HabitManager(self)
        self.project = ProjectManager(self)
        self.pomo = PomoManager(self)
        self.settings = SettingsManager(self)
        self.tag = TagsManager(self)
        self.task = TaskManager(self)
    def _prepare_session(self, username, password):
        self._login(username, password)
        self._settings()
        self.sync()
    def reset_local_state(self):
        self.state = {'projects': [], 'project_folders': [], 'tags': [],'tasks': [],'user_settings': {},'profile': {}}
    def _login(self, username: str, password: str) -> None:
        url = self.BASE_URL + 'user/signon?wc=true&remember=true'
        user_info = {'username': username, 'password': password}
        parameters = {'wc': True, 'remember': True}
        response = self.http_post(url, json=user_info, params=parameters, headers=self.HEADERS)
        self.access_token = response['token']
        self.cookies['t'] = self.access_token
    @staticmethod
    def check_status_code(response, error_message: str) -> None:
        if response.status_code != 200:
            raise RuntimeError(error_message)
    def _settings(self):
        url = self.BASE_URL + 'user/preferences/settings'
        parameters = {'includeWeb': True}
        response = self.http_get(url, params=parameters, cookies=self.cookies, headers=self.HEADERS)
        self.time_zone = response['timeZone']
        self.profile_id = response['id']
        return response
    def sync(self):
        response = self.http_get(self.INITIAL_BATCH_URL, cookies=self.cookies, headers=self.HEADERS)
        self.inbox_id = response['inboxId']
        self.state['project_folders'] = response['projectGroups']
        self.state['projects'] = response['projectProfiles']
        self.state['tasks'] = response['syncTaskBean']['update']
        self.state['tags'] = response['tags']
        return response
    def http_post(self, url, **kwargs):
        response = self._session.post(url, **kwargs)
        self.check_status_code(response, 'Could Not Complete Request')
        try:
            return response.json()
        except ValueError:
            return response.text
    def http_get(self, url, **kwargs):
        response = self._session.get(url, **kwargs)
        self.check_status_code(response, 'Could Not Complete Request')
        try:
            return response.json()
        except ValueError:
            return response.text
    def http_delete(self, url, **kwargs):
        response = self._session.delete(url, **kwargs)
        self.check_status_code(response, 'Could Not Complete Request')
        try:
            return response.json()
        except ValueError:
            return response.text
    def http_put(self, url, **kwargs):
        response = self._session.put(url, **kwargs)
        self.check_status_code(response, 'Could Not Complete Request')
        try:
            return response.json()
        except ValueError:
            return response.text
    @staticmethod
    def parse_id(response: dict) -> str:
        id_tag = response['id2etag']
        id_tag = list(id_tag.keys())
        return id_tag[0]
    @staticmethod
    def parse_etag(response: dict, multiple: bool = False) -> str:
        etag = response['id2etag']
        etag2 = list(etag.keys())
        if not multiple:
            return etag[etag2[0]]
        else:
            etags = []
            for key in range(len(etag2)):
                etags.append(etag[etag2[key]])
            return etags
    def get_by_fields(self, search: str = None, **kwargs):
        if kwargs == {}:
            raise ValueError('Must Include Field(s) To Be Searched For')
        if search is not None and search not in self.state:
            raise KeyError(f"'{search}' Is Not Present In self.state Dictionary")
        objects = []
        if search is not None:
            for index in self.state[search]:
                all_match = True
                for field in kwargs:
                    if kwargs[field] != index[field]:
                        all_match = False
                        break
                if all_match:
                    objects.append(index)
        else:
            for primarykey in self.state:
                skip_primary_key = False
                all_match = True
                middle_key = 0
                for middle_key in range(len(self.state[primarykey])):
                    if skip_primary_key:
                        break
                    for fields in kwargs:
                        if fields not in self.state[primarykey][middle_key]:
                            all_match = False
                            skip_primary_key = True
                            break
                        if kwargs[fields] == self.state[primarykey][middle_key][fields]:
                            all_match = True
                        else:
                            all_match = False
                    if all_match:
                        objects.append(self.state[primarykey][middle_key])
        if len(objects) == 1:
            return objects[0]
        else:
            return objects
    def get_by_id(self, obj_id: str, search: str = None) -> dict:
        if search is not None and search not in self.state:
            raise KeyError(f"'{search}' Is Not Present In self.state Dictionary")
        if search is not None:
            for index in self.state[search]:
                if index['id'] == obj_id:
                    return index
        else:
            for prim_key in self.state:
                for our_object in self.state[prim_key]:
                    if 'id' not in our_object:
                        break
                    if our_object['id'] == obj_id:
                        return our_object
        return {}
    def get_by_etag(self, etag: str, search: str = None) -> dict:
        if search is not None and search not in self.state:
            raise KeyError(f"'{search}' Is Not Present In self.state Dictionary")
        if search is not None:
            for index in self.state[search]:
                if index['etag'] == etag:
                    return index
        else:
            for prim_key in self.state:
                for our_object in self.state[prim_key]:
                    if 'etag' not in our_object:
                        break
                    if our_object['etag'] == etag:
                        return our_object
        return {}
    def delete_from_local_state(self, search: str = None, **kwargs) -> dict:
        if kwargs == {}:
            raise ValueError('Must Include Field(s) To Be Searched For')
        if search is not None and search not in self.state:
            raise KeyError(f"'{search}' Is Not Present In self.state Dictionary")
        if search is not None:
            for item in range(len(self.state[search])):
                all_match = True
                for field in kwargs:
                    if kwargs[field] != self.state[search][item][field]:
                        all_match = False
                        break
                if all_match:
                    deleted = self.state[search][item]
                    del self.state[search][item]
                    return deleted
        else:
            for primary_key in self.state:
                skip_primary_key = False
                all_match = True
                middle_key = 0
                for middle_key in range(len(self.state[primary_key])):
                    if skip_primary_key:
                        break
                    for fields in kwargs:
                        if fields not in self.state[primary_key][middle_key]:
                            all_match = False
                            skip_primary_key = True
                            break
                        if kwargs[fields] == self.state[primary_key][middle_key][fields]:
                            all_match = True
                        else:
                            all_match = False
                    if all_match:
                        deleted = self.state[primary_key][middle_key]
                        del self.state[primary_key][middle_key]
                        return deleted

# def _create_ticktick_client(email, password, client_id, client_secret, access_token):
def _create_ticktick_client(client_id, client_secret, access_token):
    auth_client = OAuth2(client_id=client_id, client_secret=client_secret, redirect_uri="http://127.0.0.1:8080", access_token=access_token)
    ticktick_client = TickTickClient(auth_client)
    # ticktick_client.sync()
    return ticktick_client

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up TickTickMod from a config entry."""

    client_id = entry.data[CONF_CLIENT_ID]
    client_secret = entry.data[CONF_CLIENT_SECRET]
    # email = entry.data[CONF_EMAIL]
    # password = entry.data[CONF_PASSWORD]
    access_token = entry.data.get(CONF_ACCESS_TOKEN)

    # Initialize your TickTick client
    try:
        ticktick_client = await hass.async_add_executor_job(
            # _create_ticktick_client, email, password, client_id, client_secret, access_token
            _create_ticktick_client, client_id, client_secret, access_token
        )
        
        _LOGGER.debug("Authentication successful")
        _LOGGER.debug(client_id)
        _LOGGER.debug(client_secret)
        # _LOGGER.debug(email)
        # _LOGGER.debug(password)
        _LOGGER.debug(access_token)
        
        # Log the projects
        # projects = ticktick_client.state["projects"]
        tasks = ticktick_client.task.get_from_project('5dad62dff0fe1fc4fbea252b')
        _LOGGER.debug("TickTick Tasks: %s", tasks)

    except Exception as e:
        _LOGGER.exception("Error setting up TickTickMod: %s", e)
        return False
    try:
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = ticktick_client
        # coordinator = TickTickDataUpdateCoordinator(hass, ticktick_client)
        # await coordinator._async_update_data()

        # entry.runtime_data = coordinator

        # await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception as e:
        _LOGGER.debug(e)

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.data[DOMAIN].pop(entry.entry_id)
    return True
