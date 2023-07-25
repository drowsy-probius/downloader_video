# -*- coding: utf-8 -*-
#########################################################
# imports
#########################################################
# python
import os, sys, traceback, re, json, threading
from datetime import datetime
import time
# third-party
import requests
# third-party
from flask import render_template, jsonify
from sqlalchemy import or_, desc
# sjva 공용
from framework import app, db, scheduler, path_data
from framework.util import Util
from framework.common.celery import shutil_task
from plugin import LogicModuleBase, default_route_socketio
from tool_base import ToolBaseNotify
# 패키지
from .plugin import P
logger = P.logger
ModelSetting = P.ModelSetting

#########################################################
# main logic
#########################################################

def safely_get_value_from_dict(obj, keys=[], default="None"):
  cursor = obj
  for key in keys:
    cursor = cursor.get(key)
    if cursor is None:
      return default
  return cursor


class LogicTwitch(LogicModuleBase):
  db_default = {
    'twitch_db_version': '1',
    'twitch_download_path': os.path.join(path_data, P.package_name, 'twitch'),
    'twitch_proxy_url': '',
    'twitch_auth_token': '',
    'twitch_filename_format': '[%Y-%m-%d %H:%M][{category}] {title}',
    'twitch_export_info': 'True',
    'twitch_use_ts': 'True',
    'twitch_directory_name_format': '{author} ({streamer_id})/%Y-%m',
    'twitch_file_use_segment': 'False',
    'twitch_file_segment_size': '60',
    'twitch_streamer_ids': '',
    'twitch_auto_make_folder': 'True',
    'twitch_auto_start': 'False',
    'twitch_interval': '2',
    'twitch_quality': '1080p60,best',
    'twitch_wait_for_1080': 'True',
    'twitch_wait_time': '30',
    'twitch_do_postprocess': 'False',
    'streamlink_twitch_disable_ads': 'True',
    'streamlink_twitch_disable_hosting': 'True',
    'streamlink_twitch_disable_reruns': 'True',
    'streamlink_twitch_low_latency': 'True',
    'streamlink_hls_live_edge': '2',
    'streamlink_options': 'False', # html 토글 위한 쓰레기 값임.
    'notify_discord': 'False',
    'notify_discord_webhook': '',
  }
  is_streamlink_installed = False
  streamlink_session = None # for download
  download_status = {}
  '''
  'streamer_id': {
    'streamer_id': str,
    'db_id': 0,
    'running': bool,
    'enable': bool,
    'manual_stop': bool,
    'online': bool,
    'channel_id': str,
    'author': str,
    'stream_id': str,
    'title': [],
    'category': [],
    'chapter': [],
    'url': str,
    'filepath': str, // 파일 저장 절대 경로
    'filename': str, // {part_number} 교체하기 전 이름
    'save_format': str, // segment 사용할 때 part_number가 %02d로 교체된 파일명의 full path
    'save_files: [],  // {part_number} 교체한 후 이름 목록
    'export_chapter': bool,
    'chapter_file': str, // 챕터파일 경로
    'quality': str,
    'use_ts': bool,
    'use_segment': bool,
    'do_postprocess': bool,
    'segment_size': int,
    'current_speed': str,
    'elapsed_time': str,
    'start_time': str,
    'end_time': str,
    'filesize': int,
    'filesize_str': str, // total size
    'download_speed': str, // average speed
    'status': str,
  }
  '''


  def __init__(self, P):
    super(LogicTwitch, self).__init__(P, 'setting', scheduler_desc='twitch live 생방송 확인 작업')
    self.name = 'twitch'
    default_route_socketio(P, self)


  def process_menu(self, sub, req):
    arg = P.ModelSetting.to_dict()
    arg['sub'] = self.name
    if sub in ['setting', 'status', 'list']:
      if sub == 'setting':
        job_id = f'{self.P.package_name}_{self.name}'
        arg['scheduler'] = str(scheduler.is_include(job_id))
        arg['is_running'] = str(scheduler.is_running(job_id))
        arg['is_streamlink_installed'] = self.is_streamlink_installed
        arg['streamlink_version'] = self.get_streamlink_version()
        arg['latest_streamlink_version'] = self.get_latest_streamlink_version()
        try:
          from ffmpeg.model import ModelSetting as FfmpegModelSetting
          import subprocess
          ffmpeg_path = FfmpegModelSetting.get('ffmpeg_path')
          arg['ffmpeg_version'] = subprocess.check_output([ffmpeg_path, '-version'], shell=False).decode('utf8').splitlines()[0]
        except:
          arg['ffmpeg_version'] = 'Not Installed'
      return render_template(f'{P.package_name}_{self.name}_{sub}.html', arg=arg)
    return render_template('sample.html', title=f'404: {P.package_name} - {sub}')


  def process_ajax(self, sub, req):
    try:
      if sub == 'entity_list':
        # GET /status
        return jsonify(self.download_status)
      if sub == 'toggle':
        streamer_id = req.form['streamer_id']
        command = req.form['command']
        result = {
          'previous_status': 'offline',
        }
        if command == 'disable':
          result['previous_status'] = 'online' if self.download_status[streamer_id]['online'] else 'offline'
          self.set_download_status(streamer_id, {'enable': False, 'manual_stop': True})
        else: # enable
          self.set_download_status(streamer_id, {'enable': True, 'manual_stop': False})
        return jsonify(result)
      if sub == 'install':
        self.install_streamlink()
        self.is_streamlink_installed = True
        self.set_streamlink_session()
        return jsonify({})
      if sub == 'web_list':
        # POST /list
        database = ModelTwitchItem.web_list(req)
        database['streamer_ids'] = ModelTwitchItem.get_streamer_ids()
        return jsonify(database)
      if sub == 'db_remove':
        db_id = req.form['id']
        is_running = len([
          i for i in self.download_status
          if int(self.download_status[i]['db_id']) == int(db_id) and \
            self.download_status[i]['running']
        ]) > 0
        if is_running:
          return jsonify({'ret': False, 'msg': '다운로드 중인 항목입니다.'})

        delete_file = (req.form['delete_file'] == 'true')
        if delete_file:
          save_files = ModelTwitchItem.get_file_list_by_id(db_id)
          for save_file in save_files:
            if os.path.exists(save_file):
              shutil_task.remove(save_file)
        db_return = ModelTwitchItem.delete_by_id(db_id)
        return jsonify({'ret': db_return})
      if sub == 'export_info':
        failed_items = []
        items = ModelTwitchItem.get_info_all()
        for item in items:
          try:
            self.export_info(item)
          except Exception as e:
            failed_items.append(item)
            logger.error(f'Exception: {e}')
            logger.error(traceback.format_exc())
        if len(failed_items) == 0:
          return jsonify({'ret': True})
        return jsonify({'ret': False, 'msg': str(failed_items)})
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())
      return jsonify(({'ret': False, 'msg': e}))


  def setting_save_after(self):
    streamer_ids = [sid for sid in P.ModelSetting.get_list('twitch_streamer_ids', '|') if not sid.startswith('#')]
    before_streamer_ids = [sid for sid in self.download_status]
    old_streamer_ids = [sid for sid in before_streamer_ids if sid not in streamer_ids]
    new_streamer_ids = [sid for sid in streamer_ids if sid not in before_streamer_ids]
    existing_streamer_ids = [sid for sid in streamer_ids if sid in before_streamer_ids]
    for streamer_id in old_streamer_ids:
      if self.download_status[streamer_id]['running']:
        self.set_download_status(streamer_id, {'enable': False, 'manual_stop': True})
      else:
        del self.download_status[streamer_id]
    for streamer_id in new_streamer_ids:
      self.clear_properties(streamer_id)
    for streamer_id in existing_streamer_ids:
      if not self.download_status[streamer_id]['running']:
        self.clear_properties(streamer_id)
    self.set_streamlink_session() # 옵션이 변경되었으니 세션 재설정


  def scheduler_function(self):
    '''
    라이브 체크 후 다운로드 요청
    '''
    try:
      streamer_ids = [id for id in P.ModelSetting.get_list('twitch_streamer_ids', '|') if not id.startswith('#')]
      for streamer_id in streamer_ids:
        if not self.download_status[streamer_id]['enable']:
          continue
        if self.download_status[streamer_id]['running']:
          continue
        if not self.is_online(streamer_id):
          continue
        self.set_download_status(streamer_id, {'running': True})
        t = threading.Thread(target=self.download_thread_function, args=(streamer_id, ))
        t.setDaemon(True)
        t.start()
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())


  def plugin_load(self):
    try:
      self.is_streamlink_installed = (self.get_streamlink_version() != 'Not installed')
      self.set_streamlink_session()
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())

    if not os.path.isdir(P.ModelSetting.get('twitch_download_path')):
      os.makedirs(P.ModelSetting.get('twitch_download_path'), exist_ok=True) # mkdir -p
    ModelTwitchItem.plugin_load()
    streamer_ids = [id for id in P.ModelSetting.get_list('twitch_streamer_ids', '|') if not id.startswith('#')]
    for streamer_id in streamer_ids:
      self.clear_properties(streamer_id)


  def reset_db(self):
    db.session.query(ModelTwitchItem).delete()
    db.session.commit()
    return True


  # imported from soju6jan/klive/logic_streamlink.py
  @staticmethod
  def install_streamlink():
    try:
      def func():
        import system
        import framework.common.util as CommonUtil
        commands = [['msg', u'잠시만 기다려주세요.']]
        if app.config['config']['is_py2']:
          commands.append(['echo', 'python2 이하는 지원하지 않습니다.'])
        else:
          # commands.append([sys.executable, '-m', 'pip', 'install', '--upgrade', 'pip'])
          commands.append([
            sys.executable, 
            '-m', 'pip', 'install', 
            '--user', '--upgrade', 
            'git+https://github.com/streamlink/streamlink.git'
          ])
        commands.append(['msg', u'설치가 완료되었습니다.'])
        commands.append(['msg', u'재시작이 필요합니다.'])
        system.SystemLogicCommand.start('설치', commands)
      t = threading.Thread(target=func, args=())
      t.setDaemon(True)
      t.start()
    except Exception as e:
      logger.error('Exception:%s', e)
      logger.error(traceback.format_exc())


  #########################################################

  def send_discord_message(self, text):
    webhook_url = None
    try:
      webhook_url = P.ModelSetting.get('notify_discord_webhook')
      if webhook_url == '':
        from system.model import ModelSetting as SystemModelSetting
        webhook_url = SystemModelSetting.get('notify_discord_webhook')
        if webhook_url == '':
          return
      ToolBaseNotify.send_discord_message(text, webhook_url=webhook_url)
    except Exception as e:
      logger.error(traceback.format_exc())


  def create_gql_query(self, operationName, sha256hash, **variables):
    return {
      "operationName": operationName,
      "extensions": {
        "persistedQuery": {
          "version": 1,
          "sha256Hash": sha256hash
        }
      },
      "variables": dict(**variables)
    }


  def get_latest_streamlink_version(self):
    try:
      json_text = requests.get('https://pypi.org/pypi/streamlink/json').text
      json_data = json.loads(json_text)
      return json_data['info']['version']
    except:
      return 'Unable to get the latest version'


  def get_streamlink_version(self):
    try:
      import streamlink
      return f'v{streamlink.__version__}'
    except:
      return 'Not installed'


  def get_channel_metadata(self, streamer_id):
    """
    {
      'id': '818...', 
      'author': '...', 
      'profile': 'https://.jpg', 
      'stream': {
        'id': '397268...', 
        'viewersCount': 4010, 
        '__typename': 'Stream'
      }
    } 
    """
    query = self.create_gql_query(
      "ChannelShell",
      "c3ea5a669ec074a58df5c11ce3c27093fa38534c94286dc14b68a25d5adcbf55",
      login=streamer_id,
      lcpVideosEnabled=False
    )
    metadata = {
      "id": "None",
      "author": "None",
      "profile": "None",
      "stream": "None",
    }
    try:
      res = requests.post(
        "https://gql.twitch.tv/gql", 
        data=json.dumps(query),
        headers={
          "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
        }
      )
      contents = res.json()
      if "data" not in contents:
        return metadata
      user = contents["data"]["userOrError"]
      metadata["id"] = str(user["id"]) # id는 없으면 에러 리턴하도록
      metadata["author"] = safely_get_value_from_dict(user, ["displayName"])
      metadata["profile"] = safely_get_value_from_dict(user, ["profileImageURL"])
      metadata["stream"] = safely_get_value_from_dict(user, ["stream"], default=None)
    except Exception as e:
      logger.error(f'[get_channel_metadata] {streamer_id} {metadata}')
      logger.error(f'{e}')
      logger.error(traceback.format_exc())
    finally:
      return metadata


  def get_stream_metadata(self, streamer_id):
    '''
    returns
    {'id': '39726838647', 'category': 'Just Chatting', 'categoryType': 'Game', 'title': '화질테스트 on'}
    '''
    query = self.create_gql_query(
      "StreamMetadata",
      "059c4653b788f5bdb2f5a2d2a24b0ddc3831a15079001a3d927556a96fb0517f",
      channelLogin=streamer_id
    )
    metadata = {
      "id": "None",
      "category": "None",
      "categoryType": "None",
      "title": "None",
    }
    try:
      res = requests.post(
        "https://gql.twitch.tv/gql", 
        data=json.dumps(query),
        headers={
          "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
        }
      )
      contents = res.json() 
      user = contents["data"]["user"]
      if user["stream"] is None:
        raise Exception(f"{streamer_id} is offline?")
      # metadata["id"] = user.get("lastBroadcast", {}).get("id", "None")
      metadata["id"] = user["lastBroadcast"]["id"] # id는 없으면 일부러 에러 리턴하도록
      metadata["title"] = safely_get_value_from_dict(user, ["lastBroadcast", "title"])
      metadata["category"] = safely_get_value_from_dict(user, ["stream", "game", "name"])
      metadata["categoryType"] = safely_get_value_from_dict(user, ["stream", "game", "__typename"])
    except Exception as e:
      logger.error(f'[get_stream_metadata] {user} {streamer_id} {metadata}')
      logger.error(f'{e}')
      logger.error(traceback.format_exc())
    finally:
      return metadata


  def is_online(self, streamer_id):
    '''
    return True if stream exists
    do not check metadata but just update
    '''
    channel_metadata = self.get_channel_metadata(streamer_id)
    return channel_metadata["stream"] != None


  def update_metadata(self, streamer_id):
    try:
      stream_metadata = {}
      stream_metadata = self.get_stream_metadata(streamer_id)
      if not self.download_status[streamer_id]['running']:
        raise Exception(f'{streamer_id} is not running')
      if len(self.download_status[streamer_id]['title']) < 1 or len(self.download_status[streamer_id]['category']) < 1:
        raise Exception(f'the status of {streamer_id} has not been set. {self.download_status[streamer_id]}')
      if self.download_status[streamer_id]['title'][-1] != stream_metadata['title'] or \
        self.download_status[streamer_id]['category'][-1] != stream_metadata['category']:
        logger.debug(f'[{streamer_id}] metadata updated: {stream_metadata}')
        self.set_download_status(streamer_id, {
          'title': self.download_status[streamer_id]['title'] + [stream_metadata['title']],
          'category': self.download_status[streamer_id]['category'] + [stream_metadata['category']],
          'chapter': self.download_status[streamer_id]['chapter'] + [self.download_status[streamer_id]['elapsed_time']],
        })
    except Exception as e:
      logger.error(f'Exception while downloading {streamer_id}')
      logger.error(f'{self.download_status[streamer_id]}')
      logger.error(f'{stream_metadata}')
      logger.error(f'{e}')
      logger.error(traceback.format_exc())


  def get_streams(self, streamer_id):
    '''
    returns {qualities: urls}

    옵션 값 유지하기 위해서 만들어진 세션 사용.
    vpn을 이용해서 스트림 주소를 가져옴.
    '''
    if self.streamlink_session is None:
      self.set_streamlink_session()
    # 5.0.0 이상에서는 (plugin_name, streamlink_plugin_class, url) 으로 할당해야 함.
    (plugin_name, streamlink_plugin_class, url) = self.streamlink_session.resolve_url(f'https://www.twitch.tv/{streamer_id}')
    streamlink_plugin = streamlink_plugin_class(self.streamlink_session, url)
    streams = streamlink_plugin.streams()
    return {q:streams[q] for q in streams}


  def select_stream(self, streamer_id):
    '''
    returns (quality, Streamlink stream class)
    '''
    result_quality = ''
    result_stream = None

    quality_options = [i.strip() for i in P.ModelSetting.get('twitch_quality').split(',')]
    wait_for_1080 = P.ModelSetting.get_bool('twitch_wait_for_1080')
    wait_time = P.ModelSetting.get_int('twitch_wait_time')

    streams = self.get_streams(streamer_id)
    if wait_for_1080 and quality_options[0].startswith('best') or quality_options[0].startswith('1080'):
      import time
      elapsed_time = 0
      # best가 720p와 다르면 빠져나옴.
      while elapsed_time < wait_time:
        if (("720p" not in streams) or (streams["best"] != streams["720p"])) and \
          (("720p60" not in streams) or (streams["best"] != streams["720p60"])):
          break
        logger.debug(f'[{streamer_id}] waiting for source stream.')
        time.sleep(5)
        elapsed_time = elapsed_time + 5
        streams = self.get_streams(streamer_id)

    for quality in quality_options:
      if quality in streams:
        result_quality = quality
        result_stream = streams[quality]
        break

    if len(result_quality) == 0:
      raise Exception(f'No available streams for {streamer_id} with {quality_options} in {streams}')

    if result_quality in ['best', 'worst']: # convert best -> 1080p60, worst -> 160p
      for quality in streams:
        if result_stream.url == streams[quality].url and result_quality != quality:
          result_quality = quality
          break
    return (result_quality, result_stream)


  def get_options(self):
    '''
    returns [(option1), (option2), ...]
    '''
    options = []
    http_proxy = P.ModelSetting.get('twitch_proxy_url')
    auth_token = P.ModelSetting.get('twitch_auth_token')
    streamlink_twitch_disable_ads = P.ModelSetting.get_bool('streamlink_twitch_disable_ads')
    streamlink_twitch_disable_hosting = P.ModelSetting.get_bool('streamlink_twitch_disable_hosting')
    streamlink_twitch_disable_reruns = P.ModelSetting.get_bool('streamlink_twitch_disable_reruns')
    streamlink_twitch_low_latency = P.ModelSetting.get_bool('streamlink_twitch_low_latency')
    streamlink_hls_live_edge = P.ModelSetting.get_int('streamlink_hls_live_edge')
    options = options + [
      ['hls-live-edge', streamlink_hls_live_edge],
      ['twitch', 'disable-ads', streamlink_twitch_disable_ads],
      ['twitch', 'disable-hosting', streamlink_twitch_disable_hosting],
      ['twitch', 'disable-reruns', streamlink_twitch_disable_reruns],
      ['twitch', 'low-latency', streamlink_twitch_low_latency],
    ]
    if len(http_proxy) != 0:
      options = options + [
        ['http-proxy', http_proxy],
      ]
    
    http_headers = {
      "Client-ID": "ue6666qo983tsx6so1t0vnawi233wa", # 230602 for streams
    }

    if len(auth_token) != 0:
      http_headers = {
        "Authorization": f"OAuth {auth_token}", # for adblocking
        **http_headers,
      }
    
    options = options + [
      ['twitch', 'api-header', http_headers]
    ]
    return options


  def set_streamlink_session(self):
    try:
      import streamlink
      self.streamlink_session = streamlink.Streamlink()
      options = self.get_options()
      for option in options:
        if len(option) == 2:
          self.streamlink_session.set_option(option[0], option[1])
      #  elif len(option) == 3:
      #    self.streamlink_session.set_plugin_option(option[0], option[1], option[2])
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())


  def set_filepath(self, streamer_id):
    '''
    create download directory and set filepath attribute in self.download_status[streamer_id]
    '''
    download_base_directory = P.ModelSetting.get('twitch_download_path')
    download_make_directory = P.ModelSetting.get_bool('twitch_auto_make_folder')
    filepath_format = P.ModelSetting.get('twitch_directory_name_format')
    filepath_string = ''
    if download_make_directory:
      filepath_string = '/'.join([
        self.replace_unavailable_characters_in_filename(self.parse_string_from_format(streamer_id, directory_format) )
        for directory_format in filepath_format.split('/')
      ])
    filepath = os.path.join(download_base_directory, filepath_string)
    if not os.path.isdir(filepath):
      os.makedirs(filepath, exist_ok=True)
    self.set_download_status(streamer_id, {'filepath': filepath})


  def replace_unavailable_characters_in_filename(self, source):
    replace_list = {
      ':': '∶',
      '/': '-',
      '\\': '-',
      '*': '⁎',
      '?': '？',
      '"': "'",
      '<': '(',
      '>': ')',
      '|': '_',
      '\n': '',
      '\r': '',
    }
    for key in replace_list.keys():
      source = source.replace(key, replace_list[key])
    return source


  def truncate_string_in_byte_size(self, unicode_string, size):
    # byte_string = unicode_string.encode('utf-8')
    # limit = size
    # # 
    # while (byte_string[limit] & 0xc0) == 0x80:
    #   limit -= 1
    # return byte_string[:limit].decode('utf-8')
    if len(unicode_string.encode('utf8')) > size:
      return unicode_string.encode('utf8')[:size].decode('utf8', 'ignore').strip() + '...'
    return unicode_string


  def parse_string_from_format(self, streamer_id, format_str):
    '''
    keywords: {author}, {title}, {category}, {streamer_id}, {quality}
    and time foramt keywords: %m,%d,%Y, %H,%M,%S, ...
    https://docs.python.org/ko/3/library/datetime.html#strftime-and-strptime-format-codes
    '''

    result = format_str
    result = result.replace('{streamer_id}', streamer_id)
    result = result.replace('{channel_id}', str(self.download_status[streamer_id]['channel_id']))
    result = result.replace('{stream_id}', str(self.download_status[streamer_id]['stream_id']))
    result = result.replace('{author}', str(self.download_status[streamer_id]['author']))
    result = result.replace('{category}', str(self.download_status[streamer_id]['category'][0]))
    result = result.replace('{quality}', str(self.download_status[streamer_id]['quality']))
    result = datetime.now().strftime(result)

    # in normal filesystem, filename length is 256 bytes
    # title이 가장 많은 길이를 차지할 것이라고 가정함.
    title_limit = 147
    original_title = str(self.download_status[streamer_id]['title'][0])
    length_test_result = result.replace('{title}', original_title)
    if len(length_test_result.encode('utf8')) > 224: # 256 - 32
      truncated_title = self.truncate_string_in_byte_size(original_title, title_limit)
      result = result.replace('{title}', truncated_title) 
    else:
      result = length_test_result
    result = self.replace_unavailable_characters_in_filename(result)
    return result


  def set_download_status(self, streamer_id, values: dict):
    '''
    set download_status and send socketio_callback('update')
    '''
    try:
      if streamer_id not in self.download_status:
        self.download_status[streamer_id] = {}
      for key in values:
        self.download_status[streamer_id][key] = values[key]
      ModelTwitchItem.update(self.download_status[streamer_id])
      self.socketio_callback('update', {'streamer_id': streamer_id, 'status': self.download_status[streamer_id]})
    except Exception as e:
      logger.error(f'Exception while downloading {streamer_id}')
      logger.error('Exception:%s', e)
      logger.error(traceback.format_exc())


  def clear_properties(self, streamer_id):
    '''
    clear download_status[streamer_id]
    '''
    self.clear_download_status(streamer_id)


  def clear_download_status(self, streamer_id):
    enable_value = True
    if streamer_id in self.download_status:
      enable_value = self.download_status[streamer_id]['enable']
    default_values = {
      'streamer': streamer_id,
      'db_id': -1,
      'running': False,
      'enable': enable_value,
      'manual_stop': False,
      'online': False,
      'channel_id': '',
      'author': 'No Author',
      'stream_id': '',
      'title': [],
      'category': [],
      'chapter': [],
      'url': '',
      'filepath': '',
      'filename': '',
      'save_format': '',
      'save_files': [],
      'export_chapter': P.ModelSetting.get_bool('twitch_export_info'),
      'chapter_file': '',
      'quality': '',
      'use_ts': P.ModelSetting.get_bool('twitch_use_ts'),
      'use_segment': P.ModelSetting.get_bool('twitch_file_use_segment'),
      'segment_size': P.ModelSetting.get_int('twitch_file_segment_size'),
      'current_speed': '',
      'elapsed_time': '',
      'start_time': '',
      'end_time': '',
      'filesize': 0,
      'filesize_str': '',
      'download_speed': '',
      'status': '',
    }
    self.set_download_status(streamer_id, default_values)


  def download_thread_function(self, streamer_id):
    try:
      self.set_download_status(streamer_id, {
        'online': True,
        'manual_stop': False,
      })
      channel_metadata = self.get_channel_metadata(streamer_id)
      stream_metadata = self.get_stream_metadata(streamer_id)
      logger.debug(f"[download_thread_function] {streamer_id} {channel_metadata} {stream_metadata}")

      # (quality, stream) = self.select_stream(streamer_id)
      quality = "best"

      self.set_download_status(streamer_id, {
        'channel_id': channel_metadata['id'],
        'author': channel_metadata['author'],
        'stream_id': stream_metadata['id'],
        'title': [stream_metadata['title']],
        'category': [stream_metadata['category']],
        'chapter': ['00:00:00'],
        'quality': quality,
        # 'url': stream.url,
        'url': "NA",
        'options': self.get_options(),
        'use_ts': P.ModelSetting.get_bool('twitch_use_ts'),
        'use_segment': P.ModelSetting.get_bool('twitch_file_use_segment'),
        'segment_size': P.ModelSetting.get_int('twitch_file_segment_size'),
        'do_postprocess': P.ModelSetting.get_bool('twitch_do_postprocess'),
      })

      if P.ModelSetting.get_bool('notify_discord'):
        start_messages = [
          "[ON]",
          f"{self.download_status[streamer_id]['author']}",
          "-",
          f"[{self.download_status[streamer_id]['category'][-1]}]",
          f"{self.download_status[streamer_id]['title'][-1]}",
          "@",
          f"{self.download_status[streamer_id]['quality']}",
        ]
        stream_start_message = " ".join(start_messages)
        self.send_discord_message(stream_start_message)

      # mkdir
      self.set_filepath(streamer_id)
      filename = self.parse_string_from_format(streamer_id, P.ModelSetting.get('twitch_filename_format'))
      if self.download_status[streamer_id]['use_segment']:
        if '{part_number}' in filename:
          filename = filename.replace('{part_number}', '%02d')
        else:
          filename = filename + ' part%02d'
      else:
        if '{part_number}' in filename:
          filename = filename.replace('{part_number}', '')
      db_id = ModelTwitchItem.insert(streamer_id, self.download_status[streamer_id])
      self.set_download_status(streamer_id, {
        'db_id': db_id,
        'filename': filename,
      })

      save_format = self.download_status[streamer_id]['filename']
      use_ts = self.download_status[streamer_id]['use_ts']
      if self.download_status[streamer_id]['quality'] == 'audio_only':
        if use_ts:
          save_format = save_format + '.aac'
        else:
          save_format = save_format + '.mp3'
      else:
        if use_ts:
          save_format = save_format + '.ts'
        else:
          save_format = save_format + '.mp4'
      save_format = os.path.join(self.download_status[streamer_id]['filepath'], save_format)
      self.set_download_status(streamer_id, {
        'save_format': save_format,
      })

      logger.debug(f'[{streamer_id}] start to download stream: use_segment={self.download_status[streamer_id]["use_segment"]} use_ts={use_ts}')
      downloadResult = self.download_stream_ffmpeg(streamer_id)

      if P.ModelSetting.get_bool('notify_discord'):
        end_messages = [
          "[OFF]",
          f"{self.download_status[streamer_id]['author']}",
          "-",
          f"[{self.download_status[streamer_id]['category'][-1]}]",
          f"{self.download_status[streamer_id]['title'][-1]}",
          "@",
          f"{self.download_status[streamer_id]['quality']}",
          f"=> Downloaded",
          f"{self.download_status[streamer_id]['filesize_str']}",
          "in",
          f"{self.download_status[streamer_id]['elapsed_time']}",
        ]
        stream_end_message = " ".join(end_messages)
        self.send_discord_message(stream_end_message)

      if downloadResult == -1: # 다운 시작 전에 취소됨.
        logger.debug(f"[{streamer_id}] stopped by user before download")
      else:
        logger.debug(f"[{streamer_id}] stream ended")
        if self.download_status[streamer_id]['export_chapter']:
          filepath = '.'.join(self.download_status[streamer_id]['save_files'][0].split('.')[0:-1])
          chapter_file = filepath + '.chapter.txt'
          self.set_download_status(streamer_id, {
            'chapter_file': chapter_file,
          })
          self.export_info(self.download_status[streamer_id])
          if (quality != 'audio_only') and self.download_status[streamer_id]['do_postprocess']:
            postprocess_thread = threading.Thread(target=self.ffmpeg_postprocess, args=(self.download_status[streamer_id], ))
            postprocess_thread.start()
      
      # 방송 잠깐 터진 경우에 대비해서 짧은 방송 체크
      # max_try = 3
      # for i in range(max_try):
      #   if self.is_online(streamer_id):
      #     self.scheduler_function()
      #   if i < max_try - 1: # 마지막 요청 후에는 sleep 호출하지 않음.
      #     time.sleep(4)
    except Exception as e:
      logger.error(f'Exception while downloading {streamer_id}')
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())
    finally:
      self.clear_properties(streamer_id)
      if streamer_id not in [sid for sid in P.ModelSetting.get_list('twitch_streamer_ids', '|') if not sid.startswith('#')]:
        del self.download_status[streamer_id]


  def download_stream_ffmpeg(self, streamer_id):
    '''
    이거는 subprocess로 실행하고 로그 가져오기
    '''
    def ffmpeg_log_thread(process, streamlink_process):
      def str_to_bytes(text):
        result = 0
        if 'kB' in text:
          result = int(text.split('kB')[0])
          result = result * 1024
        elif 'mB' in text:
          result = int(text.split('mB')[0])
          result = result * 1024 * 1024
        return result

      metadata_last_check_time = datetime.now()
      for line in iter(process.stdout.readline, ''):
        # line = line.strip()
        # logger.debug(line)
        try:
          try:
            if (datetime.now() - metadata_last_check_time).total_seconds() > 2 * 60:
              metadata_last_check_time = datetime.now()
              self.update_metadata(streamer_id)
          except Exception as e:
            pass

          if re.compile(r"video:(?P<videosize>\S*)\s*audio:(?P<audiosize>\S*)\s*subtitle:(?P<subsize>\S*)\s*other streams:(?P<streamsize>\S*)\s*global headers:(?P<headersize>\S*)").search(line):
            match = re.compile(r"video:(?P<videosize>\S*)\s*audio:(?P<audiosize>\S*)\s*subtitle:(?P<subsize>\S*)\s*other streams:(?P<streamsize>\S*)\s*global headers:(?P<headersize>\S*)").search(line)
            videosize = str_to_bytes(match.group('videosize'))
            audiosize = str_to_bytes(match.group('audiosize'))
            self.set_download_status(streamer_id, {
              'filesize': videosize + audiosize,
              'filesize_str': Util.sizeof_fmt(videosize + audiosize, suffix='B')
            })

          if self.download_status[streamer_id]['manual_stop']:
            if streamlink_process.poll() is None:
              streamlink_process.kill()
          elif re.compile(r'size\=\s*(?P<size>\S*)\s*time\=(?P<hour>\d{2})\:(?P<minute>\d{2})\:(?P<second>\d{2})\.(?P<milisecond>\d{2})\s*bitrate\=\s*(?P<bitrate>\S*)\s*speed\=\s*(?P<speed>\S*)x').search(line):
            match = re.compile(r'size\=\s*(?P<size>\S*)\s*time\=(?P<hour>\d{2})\:(?P<minute>\d{2})\:(?P<second>\d{2})\.(?P<milisecond>\d{2})\s*bitrate\=\s*(?P<bitrate>\S*)\s*speed\=\s*(?P<speed>\S*)x').search(line)
            elapsed_time = int(match.group('milisecond')) / 100
            elapsed_time += int(match.group('second'))
            elapsed_time += int(match.group('minute')) * 60
            elapsed_time += int(match.group('hour')) * 60 * 60
            speed_times = match.group('speed')
            current_speed = match.group('bitrate')
            filesize = match.group('size')

            is_size_exists = 'N/A' not in filesize
            is_bitrate_exits = 'N/A' not in current_speed

            if is_bitrate_exits:
              if 'kbits/s' in current_speed:
                current_speed = float(current_speed.split('kbits/s')[0])
                current_speed = current_speed * 128
              elif 'mbits/s' in current_speed:
                current_speed = float(current_speed.split('mbits/s')[0])
                current_speed = current_speed * 128 * 1024
              current_speed = Util.sizeof_fmt(current_speed, suffix='B/s')

            if is_size_exists:
              filesize = str_to_bytes(filesize)
              filesize_str = Util.sizeof_fmt(filesize, suffix='B')

            self.set_download_status(streamer_id, {
              'current_speed': current_speed if is_bitrate_exits else speed_times + 'x',
              'filesize': filesize if is_size_exists else -1,
              'filesize_str': filesize_str if is_size_exists else 'N/A',
              'elapsed_time': '%02d:%02d:%02d' % (elapsed_time/3600, (elapsed_time/60)%60, elapsed_time%60),
              'status': 'downloading',
            })
          elif re.compile(r"\[segment @ .*\] Opening '(?P<filename>.*)' for writing").search(line):
            match = re.compile(r"\[segment @ .*\] Opening '(?P<filename>.*)' for writing").search(line)
            save_file = match.group('filename')
            save_files = self.download_status[streamer_id]['save_files']
            save_files.append(save_file)
            self.set_download_status(streamer_id, {
              'save_files': save_files
            })
          elif line.find('Server returned 404 Not Found') != -1 or line.find('Unknown error') != -1:
            self.set_download_status(streamer_id, {
              'status': 'cannot access to url'
            })
          elif line.find('No such file or directory') != -1:
            self.set_download_status(streamer_id, {
              'status': 'No such file or directory',
            })
        except Exception as e:
          logger.error(f'Exception while downloading {streamer_id}')
          logger.error(f'Exception: {e}')
          logger.error(traceback.format_exc())

    import subprocess
    from ffmpeg.model import ModelSetting as FfmpegModelSetting
    ffmpeg_path = FfmpegModelSetting.get('ffmpeg_path')
    # 광고 제거를 위하여
    # 다운로드에도 프록시 사용하도록 설정 
    url = f'https://www.twitch.tv/{streamer_id}'
    quality = self.download_status[streamer_id]['quality'] 
    use_segment = self.download_status[streamer_id]['use_segment']
    segment_size = self.download_status[streamer_id]['segment_size']
    audio_only = (quality == 'audio_only')
    use_ts = self.download_status[streamer_id]['use_ts']
    save_format = self.download_status[streamer_id]['save_format']

    streamlink_options = []
    options = self.get_options()
    for option in options:
      if len(option) == 2: # global option
        streamlink_options += [f'--{option[0]}', f'{option[1]}']
      else: # twitch option
        option_string = f'--{option[0]}-{option[1]}'
        if type(option[2]) == dict:
          for k,v in option[2].items():
            streamlink_options += [option_string, f'{k}={v}']
        elif str(option[2]) not in ['True', 'False']:
          streamlink_options += [option_string, f'{option[2]}']
        elif str(option[2]) == 'True':
          streamlink_options += [option_string]

    start_time = datetime.now()
    end_time = ''
    download_speed = 'N/A'

    self.set_download_status(streamer_id, {
      'status': 'start',
      'start_time': '' if start_time is None else str(start_time).split('.', maxsplit=1)[0][2:],
      'filesize': 0,
      'filesize_str': '0B',
      'current_speed': '0B/s',
      'elapsed_time': '00:00:00',
    })

    if not use_segment:
      self.set_download_status(streamer_id, {
        'save_files': [save_format],
      })

    streamlink_command = [sys.executable, '-m', 'streamlink', '-O', url, quality] + streamlink_options 
    ffmpeg_base_command = [ffmpeg_path, '-i', '-',]
    format_option = ['-acodec', 'mp3'] if (audio_only and not use_ts) else ['-c', 'copy']
    format_option += ['-movflags', '+faststart'] if (not audio_only and not use_ts) else []
    metadata_option = ['-metadata', f'title={self.truncate_string_in_byte_size(self.download_status[streamer_id]["title"][0], 147)}']
    metadata_option += ['-metadata', f'artist={self.download_status[streamer_id]["author"]}']
    metadata_option += ['-metadata', f'genre={self.download_status[streamer_id]["category"][0]}']
    metadata_option += ['-metadata', f'date={self.download_status[streamer_id]["start_time"]}']
    segment_option = ['-f', 'segment', '-segment_time', str(segment_size*60), '-reset_timestamps', '1', '-segment_start_number', '1'] if use_segment else []
    ffmpeg_command = ffmpeg_base_command + format_option + metadata_option + segment_option + [save_format]
    
    logger.debug(streamlink_command)
    logger.debug(ffmpeg_command)

    # 다운로드 요청 전에 취소될 경우에는 -1를 리턴함
    if self.download_status[streamer_id]['manual_stop']:
      return -1

    streamlink_process = subprocess.Popen(streamlink_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    process = subprocess.Popen(
      ffmpeg_command, 
      stdin=streamlink_process.stdout,
      stdout=subprocess.PIPE, 
      stderr=subprocess.STDOUT, 
      universal_newlines=True,
      encoding="utf-8",
      errors="ignore",
    )

    log_thread = threading.Thread(target=ffmpeg_log_thread, args=(process, streamlink_process, ))
    log_thread.start()
    if log_thread is None:
      self.set_download_status(streamer_id, {
        'status': 'unknown error',
      })

    process_ret = process.wait()
    if process_ret != 0:
      logger.debug(f'process return code: {process_ret}')
      logger.error(process.stdout.readline())

    end_time = datetime.now()
    elapsed_time = (end_time - start_time).total_seconds()

    is_size_exists = (self.download_status[streamer_id]['filesize'] > 0)
    if is_size_exists:
      download_speed = Util.sizeof_fmt(self.download_status[streamer_id]['filesize']/elapsed_time, suffix='B/s')

    self.set_download_status(streamer_id, {
      'running': False,
      'end_time': '' if end_time is None else str(end_time).split('.', maxsplit=1)[0][2:],
      'elapsed_time': '%02d:%02d:%02d' % (elapsed_time/3600, (elapsed_time/60)%60, elapsed_time%60),
      'download_speed': download_speed,
    })
    if len([i for i in self.download_status[streamer_id]['save_files'] if len(i)]) == 0:
      ModelTwitchItem.delete_by_id(self.download_status[streamer_id]['db_id'])


  def export_info(self, item):
    try:
      if type(item) != type({}):
        import collections
        running = item.running
        save_files = json.loads(item.save_files, object_pairs_hook=collections.OrderedDict)
        category = json.loads(item.category, object_pairs_hook=collections.OrderedDict)
        chapter = json.loads(item.chapter, object_pairs_hook=collections.OrderedDict)
        title = json.loads(item.title, object_pairs_hook=collections.OrderedDict)
        elapsed_time = item.elapsed_time
        author = item.author
        chapter_file = item.chapter_file
        filename = item.filename
        start_time = item.start_time
        end_time = item.end_time
      else:
        running = item['running']
        save_files = item['save_files']
        category = item['category']
        chapter = item['chapter']
        title = item['title']
        elapsed_time = item['elapsed_time']
        author = item['author']
        chapter_file = item['chapter_file']
        filename = item['filename']
        start_time = item['start_time']
        end_time = item['end_time']

      if running:
        return
      if len(save_files) == 0:
        return
      if not os.path.exists(save_files[0]):
        return # db에는 있지만 로컬에는 없는 파일의 챕터 생성 방지
      if len(chapter_file) == 0:
        return

      # if os.path.exists(chapter_file):
      #   logger.debug(f'{chapter_file} already exists. overwriting...')

      running_time = 0
      [ehrs, emins, esecs] = elapsed_time.split(':')
      emins = (int(ehrs) * 60) + int(emins)
      esecs = (int(emins) * 60) + int(esecs)
      running_time = esecs * 1000

      chapter_length = len(chapter)
      chapter_info = []
      result = f""";FFMETADATA1
title={title[0]}
artist={author}
genre={category[0]}
date={start_time}
"""
      for i in range(0, chapter_length):
        [hrs, mins, secs] = chapter[i].split(':')
        mins = (int(hrs) * 60) + int(mins)
        secs = (int(mins) * 60) + int(secs)
        timestamp = (int(secs) * 1000)
        chapter_info.append({
          'timestamp': timestamp,
          'title': title[i],
          'category': category[i],
        })
      for i in range(0, chapter_length):
        start = chapter_info[i]['timestamp']
        if i+1 == chapter_length: 
          end = running_time
        else: 
          end = chapter_info[i + 1]['timestamp'] - 1
        if start == 0: 
          start = 1

        title = str(chapter_info[i]['title'])
        category = str(chapter_info[i]['category'])
        title = title.replace('=','\=').replace(';','\;').replace('#','\#').replace('\\', '\\\\').replace('\n','\\n').replace('\r','\\r')
        category = category.replace('=','\=').replace(';','\;').replace('#','\#').replace('\\', '\\\\').replace('\n','\\n').replace('\r','\\r')
        result += f"""
[CHAPTER]
TIMEBASE=1/1000
#chapter starts at {chapter[i]}
START={start}
#chapter ends at {chapter[i+1] if i+1 != chapter_length else 'video ends'}
END={end}
title={title}\\
{category}
"""
      with open(chapter_file, 'w', encoding="utf8") as f:
        f.write(result)
        f.close()
    except Exception as e:
      logger.error(f'Exception while creating chapter info of: {author}')
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())


  def ffmpeg_postprocess(self, download_status):
    """
    quality == audio_only이면 작업 안함

    다른 함수는 streamer_id를 받아서 self.download_status에 직접 접근하는 반면에
    이 후처리 함수는 복사된 객체를 인자로 받음으로써
    후처리 중에 스트리머가 새로 스트림을 시작하더라도 정상적으로 다운로드가 가능하도록 함
    """
    try:
      import subprocess
      from ffmpeg.model import ModelSetting as FfmpegModelSetting

      ffmpeg_path = FfmpegModelSetting.get('ffmpeg_path')

      postprocess_info = {}
      postprocess_info['db_id'] = download_status['db_id']
      postprocess_info['author'] = download_status['author']
      postprocess_info['do_postprocess'] = True
      postprocess_info['done_postprocess'] = False
      postprocess_info['postprocess_files'] = [
        '.'.join(f.split('.')[:-1]) + '.mp4' for f in download_status['save_files']
      ]

      postprocess_info['save_files'] = download_status['save_files']
      postprocess_info['chapter_file'] = download_status['chapter_file']
      postprocess_info['filepath'] = download_status['filepath']
      postprocess_info['filename'] = download_status['filename']
      postprocess_info['save_format'] = os.path.join(postprocess_info['filepath'], f'{postprocess_info["filename"]}.mp4')

      postprocess_info['use_segment'] = download_status['use_segment']
      postprocess_info['segment_size'] = download_status['segment_size']

      ffmpeg_command = [ffmpeg_path, '-i', postprocess_info['chapter_file'], ]
      if postprocess_info['use_segment']:
        input_str = f'concat:{"|".join(postprocess_info["save_files"])}'
        ffmpeg_command += ['-i', input_str]
        ffmpeg_command += [
          '-f', 'segment',
          '-segment_time', str(postprocess_info['segment_size']*60),
          '-reset_timestamps', '1', '-segment_start_number', '1', ]
      else:
        ffmpeg_command += ['-i', postprocess_info['save_files'][0]]
      ffmpeg_command += ['-map_metadata', '0', '-codec', 'copy', postprocess_info['save_format']]

      process_result = subprocess.run(ffmpeg_command, capture_output=True, universal_newlines=True, text=True)
      if process_result.returncode != 0:
        logger.error(f'{postprocess_info["author"]} | some error occurred while postprocessing:')
        for error_msg in process_result.stderr.split('\n'):
          logger.error(error_msg)
        raise Exception(f'postprocess error. do not remove downloaded raw files')
      
      logger.debug(f'{postprocess_info["author"]} | postprocessor done')

      shutil_task.remove(postprocess_info['chapter_file'])
      for save_file in postprocess_info['save_files']:
        shutil_task.remove(save_file)
      postprocess_info['done_postprocess'] = True
      ModelTwitchItem.update_postprocess(postprocess_info)
    except Exception as e:
      logger.error(f'Exception while postprocessing stream of: {download_status}')
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())

#########################################################
# db
#########################################################
class ModelTwitchItem(db.Model):
  __tablename__ = '{package_name}_twitch_item'.format(package_name=P.package_name)
  __table_args__ = {'mysql_collate': 'utf8_general_ci'}
  __bind_key__ = P.package_name
  id = db.Column(db.Integer, primary_key=True)
  created_time = db.Column(db.String)
  running = db.Column(db.Boolean, default=False)
  manual_stop = db.Column(db.Boolean, default=False)
  streamer_id = db.Column(db.String)
  author = db.Column(db.String)
  title = db.Column(db.String)
  category = db.Column(db.String)
  chapter = db.Column(db.String)
  export_chapter = db.Column(db.Boolean, default=False)
  chapter_file = db.Column(db.String, default="")
  filename = db.Column(db.String, default="")
  filepath = db.Column(db.String, default="")
  save_format = db.Column(db.String, default="")
  save_files = db.Column(db.String)
  use_ts = db.Column(db.Boolean)
  use_segment = db.Column(db.Boolean)
  do_postprocess = db.Column(db.Boolean, default=False)
  done_postprocess = db.Column(db.Boolean, default=False)
  postprocess_files = db.Column(db.String, default="")
  segment_size = db.Column(db.Integer)
  filesize = db.Column(db.Integer, default=0)
  filesize_str = db.Column(db.String, default='0')
  download_speed = db.Column(db.String)
  start_time = db.Column(db.String)
  end_time = db.Column(db.String)
  elapsed_time = db.Column(db.String)
  quality = db.Column(db.String)
  options = db.Column(db.String)


  def __init__(self):
    pass


  def __repr__(self):
    return repr(self.as_dict())


  def as_dict(self):
    import collections
    ret = {x.name: getattr(self, x.name) for x in self.__table__.columns}
    ret['title'] = json.loads(self.title, object_pairs_hook=collections.OrderedDict)
    ret['category'] = json.loads(self.category, object_pairs_hook=collections.OrderedDict)
    ret['chapter'] = json.loads(self.chapter, object_pairs_hook=collections.OrderedDict)
    ret['save_files'] = json.loads(self.save_files, object_pairs_hook=collections.OrderedDict)
    ret['postprocess_files'] = json.loads(self.postprocess_files, object_pairs_hook=collections.OrderedDict)
    ret['options'] = json.loads(self.options, object_pairs_hook=collections.OrderedDict)
    return ret


  def save(self):
    db.session.add(self)
    db.session.commit()

  @classmethod
  def get_by_id(cls, id):
    return db.session.query(cls).filter_by(id=id).first()


  @classmethod
  def delete_by_id(cls, id):
    db.session.query(cls).filter_by(id=id).delete()
    db.session.commit()
    return True


  @classmethod
  def get_file_list_by_id(cls, id):
    item = cls.get_by_id(id)
    if item.do_postprocess:
      return json.loads(item.postprocess_files)
    return json.loads(item.save_files)


  @classmethod
  def get_info_all(cls):
    return db.session.query(cls).with_entities(
      cls.running,
      cls.save_files,
      cls.category,
      cls.chapter,
      cls.title,
      cls.elapsed_time,
      cls.author,
      cls.chapter_file,
      cls.filename,
      cls.start_time,
      cls.end_time
    ).all()


  @classmethod
  def web_list(cls, req):
    ret = {}
    page = int(req.form['page']) if 'page' in req.form else 1
    page_size = 30
    # job_id = ''
    search = req.form['search_word'] if 'search_word' in req.form else ''
    option = req.form['option'] if 'option' in req.form else 'all'
    order = req.form['order'] if 'order' in req.form else 'desc'
    query = cls.make_query(search=search, order=order, option=option)
    count = query.count()
    query = query.limit(page_size).offset((page-1)*page_size)
    lists = query.all()
    ret['list'] = [item.as_dict() for item in lists]
    ret['paging'] = Util.get_paging_info(count, page, page_size)
    return ret


  @classmethod
  def make_query(cls, search='', order='desc', option='all'):
    query = db.session.query(ModelTwitchItem)
    conditions = []

    search = search.strip()
    if search is not None and search != '':
      if search.find('|') != -1:
        tmp = search.split('|')
        for tt in tmp:
          if tt != '':
            search_key = f'%{tt.strip()}%'
            conditions.append(cls.title.like(search_key))
            conditions.append(cls.author.like(search_key))
            conditions.append(cls.category.like(search_key))

      search_key = f'%{search}%'
      conditions.append(cls.title.like(search_key) )
      conditions.append(cls.author.like(search_key) )
      conditions.append(cls.category.like(search_key) )
      query = query.filter(or_(*conditions))

    if option != 'all':
      query = query.filter(cls.streamer_id == option)

    query = query.order_by(desc(cls.id)) if order == 'desc' else query.order_by(cls.id)
    return query


  @classmethod
  def plugin_load(cls):
    items = db.session.query(cls).filter(cls.filesize < (4 * 1024)).all() # 4kB
    for item in items:
      if item.filesize == -1: 
        continue
      save_files = cls.get_file_list_by_id(item.id)
      for save_file in save_files:
        if os.path.exists(save_file) and os.path.isfile(save_file):
          shutil_task.remove(save_file)
      cls.delete_by_id(item.id)
      logger.debug(f'dirty item ({item.filename}) deleted.')
    db.session.query(cls).update({'running': False})
    db.session.commit()


  @classmethod
  def process_done(cls, download_status):
    cls.update(download_status)
    item = cls.get_by_id(download_status['db_id'])
    item.running = False
    item.save()


  @classmethod
  def delete_empty_items(cls):
    db.session.query(cls).filter_by(filesize=0).delete()
    db.session.commit()
    return True


  @classmethod
  def get_streamer_ids(cls):
    return [item.streamer_id for item in db.session.query(cls.streamer_id).distinct()]


  @classmethod
  def insert(cls, streamer_id, initial_values):
    item = ModelTwitchItem()
    item.created_time = str(datetime.now()).split('.', maxsplit=1)[0][2:]
    item.streamer_id = streamer_id
    item.running = initial_values['running']
    item.manual_stop = initial_values['manual_stop']
    item.author = initial_values['author']
    item.title = json.dumps(initial_values['title'], ensure_ascii=False, sort_keys=False)
    # encure_ascii=False 안하면 유니코드로 저장이 되어서 한글 검색이 안됨.
    item.category = json.dumps(initial_values['category'], ensure_ascii=False, sort_keys=False)
    item.chapter = json.dumps(initial_values['chapter'], ensure_ascii=False, sort_keys=False)
    item.save_format = initial_values['save_format']
    item.filepath = initial_values['filepath']
    item.filename = initial_values['filename']
    item.export_chapter = initial_values['export_chapter']
    item.quality = initial_values['quality']
    item.use_ts = initial_values['use_ts']
    item.use_segment = initial_values['use_segment']
    item.do_postprocess = initial_values['do_postprocess']
    item.postprocess_files = json.dumps([], ensure_ascii=False, sort_keys=False)
    item.segment_size = initial_values['segment_size']
    item.options = json.dumps(initial_values['options'], ensure_ascii=False, sort_keys=False)
    item.save()
    return item.id


  @classmethod
  def update(cls, download_status):
    item = cls.get_by_id(download_status['db_id'])
    if item is None: 
      return
    item.running = download_status['running']
    item.manual_stop = download_status['manual_stop']
    item.title = json.dumps(download_status['title'], ensure_ascii=False, sort_keys=False)
    item.category = json.dumps(download_status['category'], ensure_ascii=False, sort_keys=False)
    item.chapter = json.dumps(download_status['chapter'], ensure_ascii=False, sort_keys=False)
    item.save_format = download_status['save_format']
    item.filepath = download_status['filepath']
    item.filename = download_status['filename']
    item.save_files = json.dumps(download_status['save_files'], ensure_ascii=False, sort_keys=False)
    item.chapter_file = download_status['chapter_file']
    item.filesize = download_status['filesize']
    item.filesize_str = download_status['filesize_str']
    item.download_speed = download_status['download_speed']
    item.start_time = download_status['start_time']
    item.end_time = download_status['end_time']
    item.elapsed_time = download_status['elapsed_time']
    item.save()

  @classmethod
  def update_postprocess(cls, status):
    item = cls.get_by_id(status['db_id'])
    if item is None: 
      return
    item.done_postprocess = status['done_postprocess']
    item.postprocess_files = json.dumps(status['postprocess_files'], ensure_ascii=False, sort_keys=False)
    item.save_files = json.dumps(status['save_files'], ensure_ascii=False, sort_keys=False)
    item.chapter_file = status['chapter_file']
    item.save()
