# -*- coding: utf-8 -*-
from __future__ import unicode_literals
import sys
import os
import time
from functools import partial
from argparse import ArgumentParser

import eventlet
import requests
from tqdm import tqdm
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from dglib.utils import isoformat_date, dget, to_unicode, module_path, module_file, changefileext, \
	makesure_dirpathexists, chunk
from dglib.tracer import ScreenLogger
from pyquery import PyQuery as pq

__version__ = '0.1'

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
eventlet.monkey_patch(select=True, socket=True)

APPPATH = to_unicode(module_path())
LOG_FILE = to_unicode(changefileext(module_file(), b'log'))


class Song(object):
	def __init__(self, data):
		self.data = data
		self.sid = self.data['sid']
		self.playable = self.data['is_douban_playable']
		self.album = self.data['albumtitle']
		self.name = self.data['title']
		self.artist = self.data['artist']
		self.public_time = self.data['public_time']  # 2009
		self.sha256 = self.data['sha256']
		self.url = self.data['url']

	def get_time(self, key):
		return decode_time(self.data.get(key, 0))

	def __str__(self):
		return self.__unicode__().encode(opts.encoding)

	def __unicode__(self):
		return '<%s %s_%s[%s] %s>' % (self.__class__.__name__,
									  self.sid,
									  self.name,
									  self.album,
									  self.artist)


class DoubanFm(object):
	def __init__(self, uid, pwd):
		self.uid = uid
		self.pwd = pwd
		self.already_login = False
		self.baseurl = 'https://douban.fm'
		self.session = requests.Session()
		self.session.headers.update({
			'User-Agent': 'Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
						  'Chrome/59.0.3071.115 Safari/537.36'})
		self.session.headers.update({'Referer': 'https://douban.fm'})
		self.pool = eventlet.GreenPool(100)

	def login(self):
		"""
		source=fm&referer=https%3A%2F%2Fdouban.fm%2Fuser-guide&ck=MYF5&name=&password=&captcha_solution=&captcha_id=
		"""
		rep = self.open_path('/popup/login?source=fm&use_post_message', baseurl='https://accounts.douban.com')
		doc = pq(rep.content)
		inputs = doc('form input[type="hidden"]')
		data = {ipt.name: ipt.value for ipt in inputs}
		data['name'] = self.uid
		data['password'] = self.pwd
		rep = self.open_path('/j/popup/login/basic', data=data, baseurl='https://accounts.douban.com')
		d = rep.json()
		succ = dget(d, 'status') == 'success'
		if succ:
			self.already_login = True
		return succ

	def open_path(self, path, params=None, data=None, json=None, baseurl=None, method=None):
		if not baseurl:
			baseurl = self.baseurl
		url = ''.join([baseurl, path])
		if not method:
			method = 'POST' if data or json else 'GET'
		rep = self.session.request(method, url, params=params, data=data, json=json, verify=False)
		return rep

	def get_redheart_songs_brief(self):
		if not self.already_login:
			self.login()
		# get cookie 'ck'
		'''
		return_to	https://douban.fm/j/check_loggedin?san=1
		sig	fef21efae8
		r	0.8740207991594469
		callback	_login_check_callback
		'''
		params = {'return_to': 'https://douban.fm/j/check_loggedin?san=1', 'callback': '_login_check_callback'}
		path = '/service/account/check_with_js'
		self.open_path(path, params, baseurl='https://www.douban.com')
		# get songs brief
		path = '/j/v2/redheart/basic'
		rep = self.open_path(path)
		return rep.json()['songs']

	def get_redheart_song_info_multi(self, sids):
		if not self.already_login:
			self.login()
		# noinspection PyTypeChecker
		if isinstance(sids, basestring):
			sids = [sids]
		path = '/j/v2/redheart/songs'
		data = {'sids': '|'.join(sids), 'kbps': 192, 'ck': self.session.cookies.get('ck', domain='.douban.fm')}
		rep = self.open_path(path, data=data)
		return rep.json()

	def get_redheart_songs_info(self, briefs):
		songs = []
		for data in self.pool.imap(self.get_redheart_song_info_multi, chunk(briefs, 10)):
			items = data
			songs.extend([Song(item) for item in items])
		return songs

	def download_song(self, song):
		ext = os.path.splitext(song.url)[-1]
		download_dir = APPPATH + 'download' + '/' + str(int(song.playable))
		makesure_dirpathexists(download_dir)
		name = '%s - %s - %s' % (song.artist, song.name, song.album)
		replacement = {'\\': '＼', '/': '／', ':': '：', '*': '＊', '?': '？', '"': '＂', '<': '＜', '>': '＞', '|': '｜'}
		for k, v in replacement.items():
			name = name.replace(k, v)
		save_filename = '%s/%s%s' % (download_dir, name, ext)
		save_filename = os.path.abspath(save_filename)
		result = {}
		result['song'] = song
		result['succ'] = download_file(song.url, save_filename)
		return result

	def download_songs(self, songs):
		with tqdm(total=len(songs)) as pbar:
			while songs:
				queue = songs
				songs = []
				for result in self.pool.imap(self.download_song, queue):
					if result['succ']:
						pbar.update(1)
					else:
						songs.append(result['song'])  # for retry


def timestamp():
	return int(time.time())


def decode_time(data):
	return int(data) / 1000


def printable_date(t):
	if t:
		return isoformat_date(t)
	else:
		return ' ' * 10


def download_file(url, save_filename):
	r = requests.get(url, stream=True)
	content_length = int(r.headers['content-length'])
	saved_size = 0
	with open(save_filename, b'wb') as f:
		for block in r.iter_content(chunk_size=8192):
			if block:  # filter out keep-alive new chunks
				f.write(block)
				saved_size += len(block)
			# f.flush() commented by recommendation from J.F.Sebastian
	return saved_size == content_length


dget = partial(dget, separator='.')


def main():
	sys.stdout = sys.stderr = ScreenLogger(LOG_FILE, encoding=opts.encoding)

	uid = opts.uid or os.environ.get('DOUBAN_UID')
	pwd = opts.pwd or os.environ.get('DOUBAN_PWD')
	if not all([uid, pwd]):
		parser.print_usage()
		sys.exit(1)

	douban_fm = DoubanFm(uid, pwd)
	song_briefs = douban_fm.get_redheart_songs_brief()
	sids = [briefs['sid'] for briefs in song_briefs]
	songs = douban_fm.get_redheart_songs_info(sids)
	print 'Redheart songs:', len(songs)
	for i, song in enumerate(songs):
		print (i + 1), song
	douban_fm.download_songs(songs)


if __name__ == '__main__':
	parser = ArgumentParser(prog='Douban Downloader', description='download redheart songs from dobuan.fm')
	parser.add_argument('-V', '--version', action='version', version='%%(prog)s %s' % __version__)
	parser.add_argument('-u', dest='uid', help='douban.fm username')
	parser.add_argument('-p', dest='pwd', help='douban.fm password')
	parser.add_argument('-e', dest='encoding', default='gb18030',
						help='set the encoding of console and log, default: gb18030')
	opts = parser.parse_args(sys.argv[1:])

	main()
