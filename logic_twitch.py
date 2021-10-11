# -*- coding: utf-8 -*-
#########################################################
# imports
#########################################################
# python
import os, sys, traceback, re, json, threading, base64
from datetime import datetime, timedelta
import copy
# third-party
import requests
# third-party
from flask import request, render_template, jsonify
from sqlalchemy import or_, and_, func, not_, desc
# sjva 공용
from framework import app, db, scheduler, path_data, socketio
from framework.util import Util
from framework.common.celery import shutil_task 
from plugin import LogicModuleBase, default_route_socketio
# 패키지
from .plugin import P
logger = P.logger
ModelSetting = P.ModelSetting

#########################################################
# main logic
#########################################################
'''
TODO:
audio_only 를 단순히 mp3로 저장하니까 플레이어에 따라 문제가 있음.
'''
class LogicTwitch(LogicModuleBase):
  db_default = {
    'twitch_db_version': '1',
    'twitch_use_ffmpeg': 'True',
    'twitch_download_path': os.path.join(path_data, P.package_name, 'twitch'),
    'twitch_filename_format': '[%Y-%m-%d %H:%M][{category}] {title} part{part_number}',
    'twitch_directory_name_format': '{author} ({streamer_id})/%Y-%m',
    'twitch_file_use_segment': 'True',
    'twitch_file_segment_size': '32',
    'twitch_streamer_ids': '',
    'twitch_auto_make_folder': 'True',
    'twitch_auto_start': 'False',
    'twitch_interval': '2',
    'twitch_quality': '1080p60,best',
    'streamlink_twitch_disable_ads': 'True',
    'streamlink_twitch_disable_hosting': 'True',
    'streamlink_twitch_disable_reruns': 'True',
    'streamlink_twitch_low_latency': 'True',
    'streamlink_hls_live_edge': 1,
    'streamlink_chunk_size': '128',
    'streamlink_options': 'False', # html 토글 위한 쓰레기 값임.
  }
  is_streamlink_installed = False
  streamlink_session = None
  streamlink_plugins = {}
  '''
  'streamer_id': <StreamlinkTwitchPlugin> 
  '''
  downloader = {}
  '''
  'streamer_id': <TwtichDownloader> 
  '''
  download_status = {}
  '''
  'streamer_id': {
    'db_id': 0,
    'running': bool,
    'enable': bool,
    'online': bool,
    'author': str,
    'title': str,
    'category': str,
    'url': str,
    'filepath': str, // {part_number} 교체하기 전 경로
    'filename': str, // {part_number} 교체하기 전 이름
    'save_path': str, // 다운로드 디렉토리
    'save_files: [],
    'quality': str,
    'use_segment': bool,
    'segment_size': int,
    'status': int,
    'status_str': str,
    'status_kor': str,
    'current_bitrate': str,
    'current_speed': str,
    'elapsed_time': datetime,
    'start_time': str,
    'end_time': str,
    'download_time': str,
    'filesize': int, // total size
    'filesize_str': str,
    'download_speed': str, // average speed
  }
  '''


  def __init__(self, P):
    super(LogicTwitch, self).__init__(P, 'setting', scheduler_desc='twitch 라이브 다운로드')
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
        arg['is_streamlink_installed'] = 'Installed' if self.is_streamlink_installed else 'Not installed'
      return render_template(f'{P.package_name}_{self.name}_{sub}.html', arg=arg)
    return render_template('sample.html', title=f'404: {P.package_name} - {sub}')


  def process_ajax(self, sub, req):
    try:
      if sub == 'entity_list': # status 초기화
        return jsonify(self.download_status)
      elif sub == 'toggle':
        streamer_id = req.form['streamer_id']
        command = req.form['command']
        result = {
          'previous_status': 'offline',
        }
        if command == 'disable':
          result['previous_status'] = 'online' if self.download_status[streamer_id]['online'] else 'offline'
          self.set_download_status(streamer_id, {'enable': False})
        elif command == 'enable':
          self.set_download_status(streamer_id, {'enable': True})
        return jsonify(result)
      elif sub == 'install':
        LogicTwitch._install_streamlink()
        self.is_streamlink_installed = True
        return jsonify({})
      elif sub == 'web_list': # list 탭에서 요청
        database = ModelTwitchItem.web_list(req)
        database['streamer_ids'] = ModelTwitchItem.get_streamer_ids()
        return jsonify(database)
      elif sub == 'db_remove':
        db_id = req.form['id']
        is_running = len([
          i for i in self.download_status 
          if int(self.download_status[i]['db_id']) == int(db_id) and \
            self.download_status[i]['running']
        ]) > 0
        if is_running:
          return jsonify({'ret': False, 'msg': '다운로드 중인 항목입니다.'})
        
        delete_file = req.form['delete_file'] == 'true'
        if delete_file:
          download_info = ModelTwitchItem.get_file_list_by_id(db_id)
          for filename in download_info['filenames']:
            download_path = os.path.join(download_info['directory'], filename)
            shutil_task.remove(download_path)
        db_return = ModelTwitchItem.delete_by_id(db_id)
        return jsonify({'ret': db_return})
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())
      return jsonify(({'ret': False, 'msg': e}))


  def setting_save_after(self):
    if self.streamlink_session is None:
      import streamlink
      self.streamlink_session = streamlink.Streamlink()
    streamer_ids = [id for id in P.ModelSetting.get_list('twitch_streamer_ids', '|') if not id.startswith('#')]
    before_streamer_ids = [id for id in self.streamlink_plugins]
    old_streamer_ids = [id for id in before_streamer_ids if id not in streamer_ids]
    new_streamer_ids = [id for id in streamer_ids if id not in before_streamer_ids]
    for streamer_id in old_streamer_ids: 
      if self.download_status[streamer_id]['running']:
        self.set_download_status(streamer_id, {'enable': False})
      else:
        del self.streamlink_plugins[streamer_id]
        del self.download_status[streamer_id]
    for streamer_id in new_streamer_ids:
      self.clear_properties(streamer_id)
    self.set_streamlink_options()


  def scheduler_function(self):
    '''
    여기서는 다운로드 요청만 하고
    status 갱신은 실제 다운로드 로직에서 
    '''
    try:
      if self.streamlink_session is None:
        import streamlink
        self.streamlink_session = streamlink.Streamlink()
        self.set_streamlink_options()

      streamer_ids = [id for id in P.ModelSetting.get_list('twitch_streamer_ids', '|') if not id.startswith('#')]
      for streamer_id in streamer_ids:
        if not self.download_status[streamer_id]['enable']:
          continue
        if self.download_status[streamer_id]['running']:
          continue
        if self.streamlink_plugins[streamer_id] is None:
          url = 'https://www.twitch.tv/' + streamer_id
          self.streamlink_plugins[streamer_id] = self.streamlink_session.resolve_url(url)
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
      import streamlink
      self.is_streamlink_installed = True
      self.streamlink_session = streamlink.Streamlink()
      self.set_streamlink_options()
    except:
      return False
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


  #########################################################

  # imported from soju6jan/klive/logic_streamlink.py
  @staticmethod
  def install_streamlink():
    try:
      def func():
        import system
        import framework.common.util as CommonUtil
        commands = [['msg', u'잠시만 기다려주세요.']]
        if app.config['config']['is_py2']:
          command.append(['echo', 'python2 이하는 지원하지 않습니다.'])
        else:
          commands.append([app.config['config']['pip'], 'install', '--upgrade', 'pip'])
          commands.append([app.config['config']['pip'], 'install', '--upgrade', 'streamlink'])
        commands.append(['msg', u'설치가 완료되었습니다.'])
        system.SystemLogicCommand.start('설치', commands)
      t = threading.Thread(target=func, args=())
      t.setDaemon(True)
      t.start()
    except Exception as e:
      logger.error('Exception:%s', e)
      logger.error(traceback.format_exc())


  def is_online(self, streamer_id):
    return len(self.get_streams(streamer_id)) > 0


  def get_title(self, streamer_id):
    return self.streamlink_plugins[streamer_id].get_title()


  def get_author(self, streamer_id):
    return self.streamlink_plugins[streamer_id].get_author()


  def get_category(self, streamer_id):
    return self.streamlink_plugins[streamer_id].get_category()
  

  def get_streams(self, streamer_id):
    return self.streamlink_plugins[streamer_id].streams()
  

  def get_streams_url_dict(self, streamer_id):
    streams = self.get_streams(streamer_id)
    return {q:streams[q].url for q in streams}


  def get_quality(self, streamer_id):
    quality = ''
    available_streams = self.get_streams(streamer_id)
    quality_options = [i.strip() for i in P.ModelSetting.get('twitch_quality').split(',')]
    for candidate_quality in quality_options:
      if candidate_quality in available_streams:
        quality = candidate_quality
        break
    if quality == '':
      raise Exception(f'No available streams for qualities: {quality_options}')
    if quality in ['best', 'worst']: # mostly convert best -> 1080p60, worst -> 160p
      for q in available_streams:
        if available_streams[q].url == available_streams[quality].url and q != quality:
          quality = q
          break
    return quality
  

  def get_url(self, streamer_id):
    quality = self.get_quality(streamer_id)
    return self.get_streams(streamer_id)[quality].url


  def get_options(self):
    '''
    from P.Modelsetting produces list for options list
    '''
    options = []
    streamlink_twitch_disable_ads = P.ModelSetting.get_bool('streamlink_twitch_disable_ads')
    streamlink_twitch_disable_hosting = P.ModelSetting.get_bool('streamlink_twitch_disable_hosting')
    streamlink_twitch_disable_reruns = P.ModelSetting.get_bool('streamlink_twitch_disable_reruns')
    streamlink_twitch_low_latency = P.ModelSetting.get_bool('streamlink_twitch_low_latency')
    streamlink_hls_live_edge = P.ModelSetting.get_int('streamlink_hls_live_edge')
    options = options + [
      ('twitch', 'disable-ads', streamlink_twitch_disable_ads),
      ('twitch', 'disable-hosting', streamlink_twitch_disable_hosting),
      ('twitch', 'disable-reruns', streamlink_twitch_disable_reruns),
      ('twitch', 'low-latency', streamlink_twitch_low_latency),
      ('hls-live-edge', streamlink_hls_live_edge),
    ]
    return options


  def set_streamlink_options(self):
    options = self.get_options()
    for option in options:
      if len(option) == 2:
        self.streamlink_session.set_option(option[0], option[1])
      elif len(option) == 3:
        self.streamlink_session.set_plugin_option(option[0], option[1], option[2])


  def set_save_path(self, streamer_id):
    ''' 
    make save_path and
    set 'save_path'
    '''
    download_base_directory = P.ModelSetting.get('twitch_download_path')
    download_make_directory = P.ModelSetting.get_bool('twitch_auto_make_folder')
    save_path_format = P.ModelSetting.get('twitch_directory_name_format')
    save_path_string = ''
    if download_make_directory:
      save_path_string = '/'.join([
        self.replace_unavailable_characters_in_filename(self.parse_string_from_format(streamer_id, directory_format) )
        for directory_format in save_path_format.split('/')
      ])
    save_path = os.path.join(download_base_directory, save_path_string)
    if not os.path.isdir(save_path):
      os.makedirs(save_path, exist_ok=True)
    self.set_download_status(streamer_id, {'save_path': save_path})


  def is_safe_to_start(self, streamer_id):
    '''
    check is online and
    author, title, category is string, not None and
    chunk_size > 0
    '''
    return self.download_status[streamer_id]['online'] and \
      type(self.download_status[streamer_id]['author']) == str and \
      type(self.download_status[streamer_id]['title']) == str and \
      type(self.download_status[streamer_id]['category']) == str



  def prepare_download(self, streamer_id):
    quality = ''
    filename_format = ''

    download_filename_format = P.ModelSetting.get('twitch_filename_format')

    try_index = 1
    max_try = 5
    while True:
      init_values = {
        'online': self.is_online(streamer_id),
        'author': self.get_author(streamer_id),
        'title': self.get_title(streamer_id),
        'category': self.get_category(streamer_id),
        'quality': self.get_quality(streamer_id),
        'url': self.get_url(streamer_id),
        'options': self.get_options(),
      }
      self.set_download_status(streamer_id, init_values)
      if self.is_safe_to_start(streamer_id):
        break
      if try_index > max_try:
        raise Exception(f'Cannot retrieve stream info: {streamer_id}')
      import time
      time.sleep(0.5)
      try_index += 1
    
    self.set_save_path(streamer_id)
    filename_format = self.parse_string_from_format(streamer_id, download_filename_format)

    db_id = ModelTwitchItem.insert(streamer_id, self.download_status[streamer_id])

    init_values2 = {
      'db_id': db_id,
      'filename': filename_format,
    }
    self.set_download_status(streamer_id, init_values2)
  # prepare ends


  def download_thread_function(self, streamer_id):
    def external_listener(values):
      self.set_download_status(streamer_id, values)

    self.prepare_download(streamer_id)
    url = self.download_status[streamer_id]['url']
    filename = self.download_status[streamer_id]['filename']
    save_path = self.download_status[streamer_id]['save_path']
    quality = self.download_status[streamer_id]['quality']
    use_segment = P.ModelSetting.get_bool('twitch_file_use_segment')
    segment_size = P.ModelSetting.get_int('twitch_file_segment_size')
    if P.ModelSetting.get_bool('twitch_use_ffmpeg'):
      self.downloader[streamer_id] = FfmpegTwitchDownloader(external_listener, url, filename, save_path, quality, use_segment, segment_size)
    else:
      opened_stream = self.get_streams(streamer_id)[quality].open()
      self.downloader[streamer_id] = StreamlinkTwitchDownloader(external_listener, url, filename, save_path, quality, use_segment, segment_size, opened_stream)
    self.downloader[streamer_id].start()


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
    }
    for key in replace_list.keys():
      source = source.replace(key, replace_list[key])
    return source


  def parse_string_from_format(self, streamer_id, format_str):
    '''
    keywords: {author}, {title}, {category}, {streamer_id}, {quality}
    and time foramt keywords: %m,%d,%Y, %H,%M,%S, ...
    https://docs.python.org/ko/3/library/datetime.html#strftime-and-strptime-format-codes
    '''
    result = format_str
    result = result.replace('{streamer_id}', streamer_id)
    result = result.replace('{author}', self.download_status[streamer_id]['author'])
    result = result.replace('{title}', self.download_status[streamer_id]['title'])
    result = result.replace('{category}', self.download_status[streamer_id]['category'])
    result = result.replace('{quality}', self.download_status[streamer_id]['quality'])
    result = datetime.now().strftime(result)
    result = self.replace_unavailable_characters_in_filename(result)
    return result


  def set_download_status(self, streamer_id, values: dict):
    '''
    set download_status and 
    send socketio_callback('status')
    '''
    if streamer_id not in self.download_status:
      self.download_status[streamer_id] = {}
    for key in values:
      self.download_status[streamer_id][key] = values[key]
    self.socketio_callback('update', self.download_status[streamer_id])

  
  def clear_properties(self, streamer_id):
    '''
    set None to streamlink_plugins[streamer_id]
    clear download_status[streamer_id] 
    '''
    if streamer_id in self.downloader:
      self.downloader[streamer_id].stop()
      del self.downloader[streamer_id]
    if streamer_id in self.streamlink_plugins:
      del self.streamlink_plugins[streamer_id]
    self.streamlink_plugins[streamer_id] = None
    self.clear_download_status(streamer_id)
    

  def clear_download_status(self, streamer_id):
    enable_value = True
    if streamer_id in self.download_status:
      enable_value = self.download_status[streamer_id]['enable']
    default_values = {
      'db_id': -1,
      'running': False,
      'enable': enable_value,
      'online': False,
      'author': 'No Author',
      'title': 'No Title',
      'category': 'No Category',
      'url': '',
      'filepath': '',
      'filename': '',
      'save_path': '',
      'save_files': [],
      'quality': '',
      'use_segment': P.ModelSetting.get_bool('twitch_file_use_segment'),
      'segment_size': P.ModelSetting.get_int('twitch_file_segment_size'),
      'status': 0,
      'status_str': '',
      'status_kor': '',
      'current_bitrate': '',
      'current_speed': '',
      'elapsed_time': '',
      'start_time': '',
      'end_time': '',
      'download_time': '',
      'filesize': 0,
      'filesize_str': '',
      'download_speed': '',
    }
    self.set_download_status(streamer_id, default_values)

#########################################################
# streamlink class
#########################################################


#########################################################
# entity
# plugin/ffmpeg/interface_program_ffmpeg.py 여기서 가져오고 싶은데
# 고쳐할 부분이 꽤 있음. 그래서 그냥 새로 만들래
#########################################################
class TwitchDownloader():
  '''
  다운로드 클래스
  나중에 상속받아서 thread_function하고 log_thread_function 정도만 수정하면 될 듯

  external_listener: main logic의 리스너
  url: m3u8
  filename: {part_number}를 제외하고 키워드 치환된 파일 명
  save_path: 키워드 치환된 폴더 경로
  quality: 1080p60, 1080p, ..., audio_only, ...
  use_segment: 파일 분할 옵션
  segment_size: 분할 시간 (분)
  '''
  from ffmpeg.logic import Status # ffmpeg 상관없이 상태 표시로 사용
  def __init__(self, 
    external_listener, url, filename, save_path, quality: str, 
    use_segment: bool, segment_size: int):
    self.external_listener = external_listener
    self.url = url
    self.filename = filename
    self.save_path = save_path
    self.quality = quality
    self.use_segment = use_segment
    self.segment_size = segment_size
    self.file_extension = '.mp3' if self.quality == 'audio_only' else '.mp4'
    self.filepath = self.get_filepath()
    self.save_files = []

    self.thread = None
    self.process = None
    self.log_thread = None
    self.stop = False # stop flag for naive downloader

    self.status = Status.READY
    self.current_bitrate = ''
    self.current_speed = ''
    self.start_time = None
    self.end_time = None
    self.download_time = None
    self.filesize = 0
    self.filesize_str = ''
    self.download_speed = ''
  
  def start(self):
    self.thread = threading.Thread(target=self.thread_function, args=())
    self.thread.start()
    self.start_time = datetime.now()
    return self.get_data()
  
  def stop(self):
    try:
      self.status = Status.USER_STOP
      self.stop = True
      self.kill()
      self.thread.join()
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())
  
  def kill(self):
    try:
      if self.process is not None and self.process.poll() is None:
        import psutil
        process = psutil.Process(self.process.pid)
        for proc in process.children(recursive=True):
          proc.kill()
        process.kill()
      self.send_data_to_listener()
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())
  
  def get_filepath(self):
    filepath = ''
    filename = self.filename
    if self.use_segment:
      if '{part_number}' in filename:
        filename.replace('{part_number}', '%02d')
      else:
        filename = filename + ' part%02d'
    filename = filename + self.file_extension
    filepath = os.path.join(self.save_path, filename)
    return filepath
  
  def get_data(self):
    elapsed_time = '' if self.start_time is None else str(datetime.now() - self.start_time).split('.')[0][5:]
    data = {
      'url': self.url,
      'filepath': self.filepath,
      'filename': self.filename,
      'save_path': self.save_path,
      'save_files': self.save_files,
      'quality': self.quality,
      'use_segment': self.use_segment,
      'segment_size': self.segment_size,
      'status': int(self.status),
      'status_str': self.status.name,
      'status_kor': str(self.status),
      'current_bitrate': self.current_bitrate,
      'current_speed': self.current_speed,
      'elapsed_time': elapsed_time,
      'start_time' : '' if self.start_time is None else str(self.start_time).split('.')[0][5:],
      'end_time' : '' if self.end_time is None else str(self.end_time).split('.')[0][5:],
      'download_time' : '' if self.download_time is None else '%02d:%02d' % (self.download_time.seconds/60, self.download_time.seconds%60),
    }
    if self.status == Status.COMPLETED:
      data['filesize'] = self.filesize
      data['filesize_str'] = Util.sizeof_fmt(self.filesize)
      data['download_speed'] = Util.sizeof_fmt(self.filesize/self.download_time.seconds, suffix='Bytes/Second')
    return data
  
  def send_data_to_listener(self):
    self.external_listener(self.get_data())
  
  def thread_function(self):
    pass
  
  def log_thread_function(self): # needed when subprocess called
    pass


class StreamlinkTwitchDownloader(TwitchDownloader):
  def __init__(self, 
    external_listener, url, filename, save_path, 
    quality: str, use_segment: bool, segment_size: int,
    opened_stream):
    super().__init__(external_listener, url, filename, save_path, quality, use_segment, segment_size)
    self.streamlink_stream = opened_stream
  
  def thread_function(self):
    try:
      chunk_size = 4096
      update_interval_seconds = 3

      before_time_for_speed = datetime.now()
      current_time_for_speed = 0
      before_bytes_for_speed = 0
      current_bytes_for_speed = 0

      if self.use_segment:
        stop_flag = False
        part_number = 1
        while True and (not stop_flag):
          filepath = self.filepath % part_number
          with open(filepath, 'wb') as target:
            self.save_files.append(filepath)
            logger.debug(f'Download segment files: {filepath}')
            while True:
              if self.stop:
                stop_flag = True
                break
              try:
                target.write(self.streamlink_stream.read(chunk_size))
              except Exception as e:
                logger.error(f'download exception: {e}')
                logger.error(f'streamlink cannot read chunk OR maybe stream ends OR sjva cannot write birnay file')
                stop_flag = True
                break

              self.filesize += chunk_size
              self.elapsed_time_seconds = (datetime.now() - self.start_time).total_seconds()

              current_time_for_speed = datetime.now()
              time_diff_seconds = (current_time_for_speed - before_time_for_speed).total_seconds()
              if time_diff_seconds > update_interval_seconds:
                byte_diff = self.filesize - before_bytes_for_speed
                before_time_for_speed = datetime.now()
                before_bytes_for_speed = self.filesize
                self.current_speed = self.get_speed_from_diff(time_diff_seconds, byte_diff)
                self.send_data_to_listener()
              if self.elapsed_time_seconds / 60 > (part_number * self.segment_size):
                break
            target.close()
          part_number += 1
      else:
        with open(self.filepath, 'wb') as target:
          self.save_files.append(self.filepath)
          logger.debug(f'Download single file: {self.filepath}')
          while True:
            if self.stop:
              stop_flag = True
              break
            try:
              target.write(self.streamlink_stream.read(chunk_size))
            except Exception as e:
              logger.error(f'download exception: {e}')
              logger.error(f'streamlink cannot read chunk OR maybe stream ends OR sjva cannot write birnay file')
              break
            self.filesize += chunk_size
            self.elapsed_time_seconds = (datetime.now() - self.start_time).total_seconds()
            current_time_for_speed = datetime.now()
            time_diff_seconds = (current_time_for_speed - before_time_for_speed).total_seconds()
            if time_diff_seconds > update_interval_seconds:
              byte_diff = self.filesize - before_bytes_for_speed
              before_time_for_speed = datetime.now()
              before_bytes_for_speed = self.filesize
              self.current_speed = self.get_speed_from_diff(time_diff_seconds, byte_diff)
              self.send_data_to_listener()
          target.close()
      
      self.streamlink_stream.close()
      del self.streamlink_stream
      if len(self.save_files) > 0: # 어떤 이유로 종료되었는데 쓰레기 파일은 존재할 때
        last_filepath = self.save_files[-1]
        if os.path.isfile(last_filepath) and os.path.getsize(last_filepath) < (512 * 1024):
          shutil_task.remove(last_filepath)
          self.save_files = self.save_files[:-1]
          self.send_data_to_listener()
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())
    
  def get_speed_from_diff(time_diff, byte_diff):
    return Util.sizeof_fmt(byte_diff/time_diff, suffix='Bytes/Second')


class FfmpegTwitchDownloader(TwitchDownloader):
  from ffmpeg.logic import Status
  from ffmpeg.model import ModelSetting as ffmpegModelSetting
  def __init__(self, 
    external_listener, url, filename, save_path,
    quality:str, use_segment: bool, segment_size: int):
    super().__init__(external_listener, url, filename, save_path, quality, use_segment, segment_size)
    self.ffmpeg_bin = ffmpegModelSetting.get('ffmpeg_path')
  
  def thread_function(self):
    try:
      import subprocess
      command = [self.ffmpeg_path, '-y', '-i', self.url, ]
      if self.quality == "audio_only":
        command = command + [
          '-c', 'copy'
        ]
      else:
        command = command + [
          '-c', 'copy',
          '-bsf:a', 'aac_adtstoasc'
        ]
      if self.use_segment:
        command = command + [
          '-f', 'segment',
          '-segment_time', self.segment_size * 60,
        ]
      command = command + [self.filepath]

      self.process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        encoding='utf8'
      )
      self.log_thread = threading.Thread(target=self.log_thread_function, args=())
      self.log_thread.start()
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())
  
  def log_thread_function(self):
    with self.process.stdout:
      for line in iter(self.process.stdout.readline, b''):
        try:
          if self.status == Status.READY:
            if line.find('Server returned 404 Not Found') != -1 or line.find('Unknown error') != -1:
              self.status = ffmpeg.WRONG_URL
              continue # 남은 line 있을 수 있으니 continue
            if line.find('No such file or directory') != -1:
              self.status = ffmpeg.WRONG_DIRECTORY
              continue

            # 이거 전체 길이 계산하는 것같은데 m3u8에서도 작동 함? 
            match = re.compile(r'Duration\:\s(\d{2})\:(\d{2})\:(\d{2})\.(\d{2})\,\sstart').search(line)
            if match:
              self.duration_str = f'{match.group(1)}:{match.group(2)}:{match.group(3)}'
              self.duration = int(match.group(4))
              self.duration += int(match.group(3)) * 100
              self.duration += int(match.group(2)) * 100 * 60
              self.duration += int(match.group(1)) * 100 * 60 * 60
              if match:
                self.status = Status.READY
                self.send_data_to_listener()
              continue
            # 다운로드 첫 시작 지점
            match = re.compile(r'time\=(\d{2})\:(\d{2})\:(\d{2})\.(\d{2})\sbitrate\=\s*(?P<bitrate>\d+).*?[$|\s](\s?speed\=\s*(?P<speed>.*?)x)?').search(line)
            if match:
              self.status = Status.DOWNLOADING
              self.send_data_to_listener()
          elif self.status == Status.DOWNLOADING:
            if line.find('HTTP error 403 Forbidden') != -1:
              self.status = Status.HTTP_FORBIDDEN
              self.kill()
              continue

            match = re.compile(r'time\=(\d{2})\:(\d{2})\:(\d{2})\.(\d{2})\sbitrate\=\s*(?P<bitrate>\d+).*?[$|\s](\s?speed\=\s*(?P<speed>.*?)x)?').search(line)
            if match: 
              self.current_duration = int(match.group(4))
              self.current_duration += int(match.group(3)) * 100
              self.current_duration += int(match.group(2)) * 100 * 60
              self.current_duration += int(match.group(1)) * 100 * 60 * 60
              try:
                self.percent = int(self.current_duration * 100 / self.duration)
              except: pass
              self.current_bitrate = match.group('bitrate')
              self.current_speed = match.group('speed')
              self.download_time = datetime.now() - self.start_time
              self.send_data_to_listener()
              continue
            # m3u8 끝날 때 직접 확인하기
            match = re.compile(r'video\:\d*kB\saudio\:\d*kB').search(line)
            if match:
              self.status = Status.COMPLETED
              self.end_time = datetime.now()
              self.download_time = self.end_time - self.start_time
              self.send_data_to_listener()
              continue
        except Exception as e:
          logger.error(f'Exception: {e}')
          logger.error(traceback.format_exc())
    
    # stdout 끝났을 때 여기서 프로세스 종료해도 될려나
    self.log_thread = None
    self.kill()


#########################################################
# db
#########################################################
class ModelTwitchItem(db.Model):
  __tablename__ = '{package_name}_twitch_item'.format(package_name=P.package_name)
  __table_args__ = {'mysql_collate': 'utf8_general_ci'}
  __bind_key__ = P.package_name
  id = db.Column(db.Integer, primary_key=True)
  created_time = db.Column(db.DateTime)
  running = db.Column(db.Boolean, default=False)
  status = db.Column(db.Integer)
  streamer_id = db.Column(db.String)
  author = db.Column(db.String)
  title = db.Column(db.String)
  category = db.Column(db.String)
  save_path = db.Column(db.String)
  save_files = db.Column(db.String)
  use_segment = db.Column(db.Boolean)
  segment_size = db.Column(db.Integer)
  filesize = db.Column(db.BigInteger)
  filesize_str = db.Column(db.String)
  download_time = db.Column(db.String)
  download_speed = db.Column(db.String)
  start_time = db.Column(db.DateTime)
  end_time = db.Column(db.DateTime)
  elapsed_time = db.Column(db.DateTime)
  quality = db.Column(db.String)
  options = db.Column(db.String)


  def __init__(self):
    pass

  def __repr__(self):
    return repr(self.as_dict())

  def as_dict(self):
    ret = {x.name: getattr(self, x.name) for x in self.__table__.columns}
    ret['created_time'] = self.created_time.strftime('%Y-%m-%d %H:%M')
    ret['start_time'] = self.start_time.strftime('%Y-%m-%d %H:%M')
    ret['end_time'] = self.end_time.strftime('%Y-%m-%d %H:%M')
    ret['elapsed_time'] = self.elapsed_time.strftime('%Y-%m-%d %H:%M')
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
    filenames = item.save_files.split('\n')
    return {
      "save_path": item.save_path,
      "save_files": filenames,
    }

  @classmethod
  def web_list(cls, req):
    ret = {}
    page = int(req.form['page']) if 'page' in req.form else 1
    page_size = 30
    job_id = ''
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
    query = db.session.query(cls)
    conditions = []

    if search is not None and search != '':
      if search.find('|') != -1:
        tmp = search.split('|')
        for tt in tmp:
          if tt != '':
            conditions.append(cls.title.like('%'+tt.strip()+'%') )
            conditions.append(cls.author.like('%'+tt.strip()+'%') )
            conditions.append(cls.category.like('%'+tt.strip()+'%') )
      elif search.find(',') != -1:
        tmp = search.split(',')
        for tt in tmp:
          if tt != '':
            conditions.append(cls.title.like('%'+tt.strip()+'%') )
            conditions.append(cls.author.like('%'+tt.strip()+'%') )
            conditions.append(cls.category.like('%'+tt.strip()+'%') )
      else:
        conditions.append(cls.title.like('%'+search+'%') )
        conditions.append(cls.author.like('%'+search+'%') )
        conditions.append(cls.category.like('%'+search+'%') )
      query = query.filter(or_(*conditions))
    
    if option != 'all':
      query = query.filter(cls.streamer_id == option)

    query = query.order_by(desc(cls.id)) if order == 'desc' else query.order_by(cls.id)
    return query


  @classmethod
  def plugin_load(cls):
    items = db.session.query(cls).filter(cls.filesize < (512 * 1024)).all()
    for item in items:
      file_list = cls.get_file_list_by_id(item.id)
      directory = file_list['directory']
      filenames = file_list['filenames']
      for filename in filenames:
        filepath = os.path.join(directory, filename)
        if os.path.exists(filepath) and os.path.isfile(filepath):
          shutil_task.remove(filepath)
      cls.delete_by_id(item.id)
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
    db.session.query(cls).filter_by(filesize="No Size").delete()
    db.session.commit()
    return True
  

  @classmethod
  def get_streamer_ids(cls):
    return [item.streamer_id for item in db.session.query(cls.streamer_id).distinct()]


  @classmethod
  def insert(cls, streamer_id, initial_values):
    item = ModelTwitchItem()
    item.streamer_id = streamer_id
    item.running = initial_values['running']
    item.status = initial_values['status']
    item.author = initial_values['author']
    item.title = initial_values['title']
    item.category = initial_values['category']
    item.save_path = initial_values['save_path']
    item.quality = initial_values['quality']
    item.use_segment = initial_values['use_segment']
    item.segment_size = initial_values['segment_size']
    item.options = '\n'.join([' '.join(str(j) for j in i) for i in initial_values['options']])
    item.save()
    return item.id


  @classmethod
  def update(cls, download_status):
    item = cls.get_by_id(download_status['db_id'])
    item.running = download_status['running']
    item.status = download_status['status']
    item.save_files = '\n'.join(download_status['save_files'])
    item.filesize = download_status['filesize']
    item.filesize_str = download_status['filesize_str']
    item.download_time = download_status['download_time']
    item.download_speed = download_status['donwload_speed']
    item.start_time = download_status['start_time']
    item.end_time = download_status['end_time']
    item.elapsed_time = download_status['elapsed_time']
    item.save()
  
