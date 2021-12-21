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
class LogicTwitch(LogicModuleBase):
  db_default = {
    'twitch_db_version': '1',
    'twitch_use_ffmpeg': 'False',
    'twitch_download_path': os.path.join(path_data, P.package_name, 'twitch'),
    'twitch_filename_format': '[%Y-%m-%d %H:%M][{category}] {title} part{part_number}',
    'twitch_directory_name_format': '{author} ({streamer_id})/%y%m',
    'twitch_file_use_segment': 'True',
    'twitch_file_segment_size': '32',
    'twitch_streamer_ids': '',
    'twitch_auto_make_folder': 'True',
    'twitch_auto_start': 'False',
    'twitch_interval': '2',
    'twitch_quality': '1080p60,best',
    'twitch_wait_for_1080': 'True',
    'twitch_wait_time': '60',
    'streamlink_twitch_disable_ads': 'True',
    'streamlink_twitch_disable_hosting': 'True',
    'streamlink_twitch_disable_reruns': 'True',
    'streamlink_twitch_low_latency': 'True',
    'streamlink_hls_live_edge': 3,
    'streamlink_chunk_size': '4096', # 4KB
    'streamlink_options': 'False', # html 토글 위한 쓰레기 값임.
  }
  is_streamlink_installed = False
  streamlink_session = None # for download
  download_status = {}
  '''
  'streamer_id': {
    'db_id': 0,
    'running': bool,
    'enable': bool,
    'manual_stop': bool,
    'online': bool,
    'author': str,
    'title': str,
    'category': str,
    'url': str,
    'filepath': str, // 파일 저장 절대 경로
    'filename': str, // {part_number} 교체하기 전 이름
    'save_files: [],  // {part_number} 교체한 후 이름 목록
    'quality': str,
    'use_segment': bool,
    'segment_size': int,
    'current_speed': str,
    'elapsed_time': str,
    'start_time': str,
    'end_time': str,
    'filesize': int, // total size
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
        arg['is_streamlink_installed'] = self.is_streamlink_installed
        arg['streamlink_version'] = self.get_streamlink_version()
      return render_template(f'{P.package_name}_{self.name}_{sub}.html', arg=arg)
    return render_template('sample.html', title=f'404: {P.package_name} - {sub}')


  def process_ajax(self, sub, req):
    try:
      if sub == 'entity_list': 
        # GET /status
        return jsonify(self.download_status)
      elif sub == 'toggle':
        streamer_id = req.form['streamer_id']
        command = req.form['command']
        result = {
          'previous_status': 'offline',
        }
        if command == 'disable':
          result['previous_status'] = 'online' if self.download_status[streamer_id]['online'] else 'offline'
          self.set_download_status(streamer_id, {'enable': False, 'manual_stop': True})
        elif command == 'enable':
          self.set_download_status(streamer_id, {'enable': True, 'manual_stop': False})
        return jsonify(result)
      elif sub == 'install':
        self.install_streamlink()
        self.is_streamlink_installed = True
        self.set_streamlink_session()
        return jsonify({})
      elif sub == 'web_list':
        # POST /list
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
        
        delete_file = (req.form['delete_file'] == 'true')
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
    streamer_ids = [sid for sid in P.ModelSetting.get_list('twitch_streamer_ids', '|') if not sid.startswith('#')]
    before_streamer_ids = [sid for sid in self.download_status]
    old_streamer_ids = [sid for sid in before_streamer_ids if sid not in streamer_ids]
    new_streamer_ids = [sid for sid in streamer_ids if sid not in before_streamer_ids]
    for streamer_id in old_streamer_ids: 
      if self.download_status[streamer_id]['running']:
        # keep current session and disable it until reboot
        self.set_download_status(streamer_id, {'enable': False})
      else:
        del self.download_status[streamer_id]
    for streamer_id in new_streamer_ids:
      self.clear_properties(streamer_id)


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
      self.is_streamlink_installed = True
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
        commands.append(['msg', u'재시작이 필요합니다.'])
        system.SystemLogicCommand.start('설치', commands)
      t = threading.Thread(target=func, args=())
      t.setDaemon(True)
      t.start()
    except Exception as e:
      logger.error('Exception:%s', e)
      logger.error(traceback.format_exc())


  def get_streamlink_version(self):
    try:
      import streamlink
      return f'v{streamlink.__version__}'
    except:
      return 'Not installed'


  def is_online(self, streamer_id):
    '''
    return True if stream exists and streaming author is not None
    '''
    return len(self.get_streams(streamer_id)) > 0 and self.get_metadata(streamer_id)['author'] is not None


  def get_metadata(self, streamer_id):
    '''
    returns
    {'id': '44828369517', 'author': 'heavyRainism', 'category': 'The King of Fighters XV', 'title': '호우!'} 

    매번 새로운 값을 가져오기 위해서 세션 새로 생성
    '''
    import streamlink
    (streamlink_plugin_class, url) = streamlink.Streamlink().resolve_url(f'https://www.twitch.tv/{streamer_id}')
    streamlink_plugin = streamlink_plugin_class(url)
    return streamlink_plugin.get_metadata()


  def get_streams(self, streamer_id):
    '''
    returns {qualities: urls} 
    
    옵션 값 유지하기 위해서 만들어진 세션 사용
    '''
    if self.streamlink_session is None:
      self.set_streamlink_session()
    (streamlink_plugin_class, url) = self.streamlink_session.resolve_url(f'https://www.twitch.tv/{streamer_id}')
    streamlink_plugin = streamlink_plugin_class(url)
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
      quality_exists = False
      while elapsed_time < wait_time:
        for quality in streams:
          if quality.startswith('1080'):
            quality_exists = True
            break
        if quality_exists:
          break
        logger.debug(f'[{streamer_id}] waiting for 1080p60 stream.')
        time.sleep(10)
        elapsed_time = elapsed_time + 10
        streams = self.get_streams(streamer_id)
    
    for quality in quality_options:
      if quality in streams:
        result_quality = quality
        result_stream = streams[quality]
        break
    
    if len(result_quality) == 0:
      raise Exception(f'No available streams for {streamer_id} with {quality_options}')
    
    if result_quality in ['best', 'worst']: # convert best -> 1080p60, worst -> 160p
      for quality in streams:
        if result_stream.url == streams[quality].url and result_quality != quality:
          result_quality = quality
          break
    return (result_quality, result_stream)


  def get_options(self, toString=False):
    '''
    returns [(option1), (option2), ...]
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
    if toString:
      return '\n'.join([' '.join([str(j) for j in i]) for i in options])
    return options


  def set_streamlink_session(self):
    try:
      if self.streamlink_session is None:
        import streamlink
        self.streamlink_session = streamlink.Streamlink()
      options = self.get_options()
      for option in options:
        if len(option) == 2:
          self.streamlink_session.set_option(option[0], option[1])
        elif len(option) == 3:
          self.streamlink_session.set_plugin_option(option[0], option[1], option[2])
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
      'db_id': -1,
      'running': False,
      'enable': enable_value,
      'manual_stop': False,
      'online': False,
      'author': 'No Author',
      'title': 'No Title',
      'category': 'No Category',
      'url': '',
      'filepath': '',
      'filename': '',
      'save_files': [],
      'quality': '',
      'use_segment': P.ModelSetting.get_bool('twitch_file_use_segment'),
      'segment_size': P.ModelSetting.get_int('twitch_file_segment_size'),
      'current_speed': '',
      'elapsed_time': '',
      'start_time': '',
      'end_time': '',
      'filesize': '',
      'download_speed': '',
    }
    self.set_download_status(streamer_id, default_values)


  def download_thread_function(self, streamer_id):
    metadata = self.get_metadata(streamer_id)
    (quality, stream) = self.select_stream(streamer_id)
    self.set_download_status(streamer_id, {
      'online': True,
      'manual_stop': False,
      'author': metadata['author'],
      'title': metadata['title'],
      'category': metadata['category'],
      'quality': quality,
      'url': stream.url,
      'options': self.get_options(toString=True),
      'use_segment': P.ModelSetting.get_bool('twitch_file_use_segment'),
      'segment_size': P.ModelSetting.get_int('twitch_file_segment_size'),
    })
    # mkdir
    self.set_filepath(streamer_id)
    filename = self.parse_string_from_format(streamer_id, P.ModelSetting.get('twitch_filename_format'))
    db_id = ModelTwitchItem.insert(streamer_id, self.download_status[streamer_id])
    self.set_download_status(streamer_id, {
      'db_id': db_id,
      'filename': filename,
    })
    self.download_stream(streamer_id, stream)
  

  def download_stream(self, streamer_id, stream):
    try:
      def get_save_file_format(path, filename, ext, use_segment):
        save_file_format = ''
        filename = filename
        if use_segment:
          if '{part_number}' in filename:
            filename = filename.replace('{part_number}', '%02d')
          else:
            filename = filename + ' part%02d'
        filename = filename + ext
        save_file_format = os.path.join(path, filename)
        return save_file_format
          
      # set initial status values
      file_extension = '.mp3' if self.download_status[streamer_id]['quality'] == 'audio_only' else '.mp4'
      filename = self.download_status[streamer_id]['filename']
      filepath = self.download_status[streamer_id]['filepath']
      use_segment = self.download_status[streamer_id]['use_segment']
      segment_size = self.download_status[streamer_id]['segment_size']
      save_file_format = get_save_file_format(filepath, filename, file_extension, use_segment)
      save_files = []

      current_speed = 0
      elapsed_time = 0
      start_time = datetime.now()
      end_time = None
      filesize = 0
      download_speed = ''

      # 다운로드와 관련된 값 선언
      # chunk_size = 4096 * 1024 # 4k bytes
      chunk_size = P.ModelSetting.get_int('streamlink_chunk_size')
      status_update_interval = 3

      before_time_for_speed = datetime.now()
      before_bytes_for_speed = 0

      self.set_download_status(streamer_id, {
        'save_files': save_files,
        'current_speed': current_speed,
        'elapsed_time': '%02d:%02d:%02d' % (elapsed_time/3600, elapsed_time/60, elapsed_time%60),
        'start_time': '' if start_time is None else str(start_time).split('.')[0][5:],
        'filesize': '' if filesize is None else Util.sizeof_fmt(filesize, suffix='B'),
        'download_speed': '0',
      })

      opened_stream = stream.open()
      if use_segment:
        part_number = 0
        while not self.download_status[streamer_id]['manual_stop']:
          part_number += 1
          save_file = save_file_format % part_number
          with open(save_file, 'wb') as target:
            save_files.append(save_file)
            logger.debug(f'[{streamer_id}] Start to download stream part {part_number}')
            error_count = 0
            while not self.download_status[streamer_id]['manual_stop']:
              try:
                target.write(opened_stream.read(chunk_size))
              except Exception as e:
                logger.error(f'[{streamer_id}] streamlink cannot read chunk. error count {error_count}')
                logger.error(f'[{streamer_id}] exception: {e}')
                error_count += 1
                if error_count > 5:
                  logger.error(f'[{streamer_id}] Stopping download stream')
                  self.set_download_status(streamer_id, {
                    'manual_stop': True
                  })
                  break
              
              filesize += chunk_size
              elapsed_time = (datetime.now() - start_time).total_seconds()

              time_diff = (datetime.now() - before_time_for_speed).total_seconds()
              if time_diff > status_update_interval:
                byte_diff = filesize - before_bytes_for_speed
                before_time_for_speed = datetime.now()
                before_bytes_for_speed = filesize 
                current_speed = Util.sizeof_fmt(byte_diff/time_diff, suffix='B/s')
                download_speed = Util.sizeof_fmt(filesize/elapsed_time, suffix='B/s')
                self.set_download_status(streamer_id, {
                  'save_files': save_files,
                  'current_speed': current_speed,
                  'elapsed_time': '%02d:%02d:%02d' % (elapsed_time/3600, elapsed_time/60, elapsed_time%60),
                  'start_time': '' if start_time is None else str(start_time).split('.')[0][5:],
                  'filesize': '' if filesize is None else Util.sizeof_fmt(filesize, suffix='B'),
                  'download_speed': '',
                })
              # next part_number
              if elapsed_time / 60 > (part_number * segment_size):
                break
            target.close()
      else:
        with open(save_file, 'wb') as target:
          save_files.append(save_file)
          logger.debug(f'[{streamer_id}] Start to download stream')
          while not self.download_status[streamer_id]['manual_stop']:
            error_count = 0
            try:
              target.write(opened_stream.read(chunk_size))
            except Exception as e:
              logger.error(f'[{streamer_id}] streamlink cannot read chunk. error count {error_count}')
              logger.error(f'[{streamer_id}] exception: {e}')
              error_count += 1
              if error_count > 5:
                logger.error(f'[{streamer_id}] Stopping download stream')
                self.set_download_status(streamer_id, {
                  'manual_stop': True
                })
                break
            
            filesize += chunk_size
            elapsed_time = (datetime.now() - start_time).total_seconds()

            time_diff = (datetime.now() - before_time_for_speed).total_seconds()
            if time_diff > status_update_interval:
              byte_diff = filesize - before_bytes_for_speed
              before_time_for_speed = datetime.now()
              before_bytes_for_speed = filesize 
              current_speed = Util.sizeof_fmt(byte_diff/time_diff, suffix='B/s')
              download_speed = Util.sizeof_fmt(filesize/elapsed_time, suffix='B/s')
              self.set_download_status(streamer_id, {
                'save_files': save_files,
                'current_speed': current_speed,
                'elapsed_time': '%02d:%02d:%02d' % (elapsed_time/3600, elapsed_time/60, elapsed_time%60),
                'start_time': '' if start_time is None else str(start_time).split('.')[0][5:],
                'filesize': '' if filesize is None else Util.sizeof_fmt(filesize, suffix='B'),
                'download_speed': '',
              })
            # next part_number
            if elapsed_time / 60 > (part_number * segment_size):
              break
          target.close()
      opened_stream.close()
      del opened_stream
      del stream 

      end_time = datetime.now()
      elapsed_time = (end_time - start_time).total_seconds()
      download_speed = Util.sizeof_fmt(filesize/elapsed_time, suffix='B/s')
      self.set_download_status(streamer_id, {
        'running': False,
        'elapsed_time': elapsed_time,
        'download_speed': download_speed,
        'end_time': '' if end_time is None else str(end_time).split('.')[0][5:],
      })

      if len(save_files) > 0:
        last_file = save_files[-1]
        if os.path.isfile(last_file) and os.path.getsize(last_file) < (512 * 1024):
          shutil_task.remove(last_file)
          save_files = save_files[:-1]
          self.set_download_status(streamer_id, {
            'save_files': save_files
          })
      
      self.clear_download_status(streamer_id)
    except Exception as e:
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
  created_time = db.Column(db.DateTime)
  running = db.Column(db.Boolean, default=False)
  manual_stop = db.Column(db.Boolean, default=False)
  streamer_id = db.Column(db.String)
  author = db.Column(db.String)
  title = db.Column(db.String)
  category = db.Column(db.String)
  save_files = db.Column(db.String)
  use_segment = db.Column(db.Boolean)
  segment_size = db.Column(db.Integer)
  filesize = db.Column(db.String, default='0')
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
    ret = {x.name: getattr(self, x.name) for x in self.__table__.columns}
    ret['created_time'] = self.created_time.strftime('%Y-%m-%d %H:%M')
    # ret['start_time'] = self.start_time.strftime('%Y-%m-%d %H:%M')
    # ret['end_time'] = self.end_time.strftime('%Y-%m-%d %H:%M')
    # ret['elapsed_time'] = self.elapsed_time.strftime('%Y-%m-%d %H:%M')
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
      "save_files": filenames,
    }

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
    items = db.session.query(cls).filter(cls.filesize < (32 * 1024)).all()
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
    item.created_time = datetime.now()
    item.streamer_id = streamer_id
    item.running = initial_values['running']
    item.manual_stop = initial_values['manual_stop']
    item.author = initial_values['author']
    item.title = initial_values['title']
    item.category = initial_values['category']
    item.quality = initial_values['quality']
    item.use_segment = initial_values['use_segment']
    item.segment_size = initial_values['segment_size']
    item.options = initial_values['options']
    item.save()
    return item.id


  @classmethod
  def update(cls, download_status):
    item = cls.get_by_id(download_status['db_id'])
    if item is None: return
    item.running = download_status['running']
    item.manual_stop = download_status['manual_stop']
    item.save_files = '\n'.join(download_status['save_files'])
    item.filesize = download_status['filesize']
    item.download_speed = download_status['download_speed']
    item.start_time = download_status['start_time']
    item.end_time = download_status['end_time']
    item.elapsed_time = download_status['elapsed_time']
    item.save()
  
