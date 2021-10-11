#!/usr/bin/python3

"""A dead-simple script to cast media files to ChromeCasts and compatibles."""

from argparse import ArgumentParser
from http.server import SimpleHTTPRequestHandler, HTTPServer
from mimetypes import guess_type
from os import fstat, get_terminal_size
from os.path import expanduser, abspath
from re import compile as re_compile
from socket import socket, AF_INET, SOCK_DGRAM
from threading import Thread
from time import sleep, time
from pydub.utils import mediainfo

from pychromecast import get_chromecasts, Chromecast
from pychromecast.discovery import stop_discovery
from pychromecast.controllers.media import MediaController
from pychromecast.error import NotConnected

FILE_COPY_BUFFER_SIZE = 64 * 1024
# https://github.com/craig-chasseur/simplecast

# A global variable containing the path of the single file to be served via
# HTTP.

global global_single_file
global_single_file = None

class SingleFileHTTPRequestHandler(SimpleHTTPRequestHandler):
  """HTTP request handler that only serves a single file and supports range."""
  
  def do_HEAD(self):
    self.range_start, self.range_end = self._GetRange()
    media_file = self.send_head()
    if media_file:
      media_file.close()
  
  def log_message(self, *args):
    return
  
  def log_request(self, *args):
    return
  
  def do_GET(self):
    self.range_start, self.range_end = self._GetRange()
    media_file = self.send_head()
    if media_file:
      try:
        if self.range_start is None:
          self.copyfile(media_file, self.wfile)
        else:
          self.copy_range(media_file, self.wfile)
      finally:
        media_file.close()
  
  def send_head(self):
    """Sends header common to HEAD and GET requests.
    
    Returns:
      file, an open file containing the content to serve.
    """
    if self.path != "/file":
      self.send_error(404, "File not found")
      return None
    
    try:
      single_file = open(global_single_file, 'rb')
    except OSError:
      self.send_error(404, "File not found")
      return None
    
    try:
      file_stat = fstat(single_file.fileno())
      if self.range_start is None:
        self._SendRegularHeaders(file_stat)
      else:
        self._SendRangeHeaders(file_stat)
    except:
      single_file.close()
      raise
    return single_file
  
  def copy_range(self, source, outputfile):
    """Copies the file range from range_start to range_end to output.
  
    Args:
      source: file, The open source file to serve.
      outputfile: file, The open output file (i.e. socket) to write to.
    """
    source.seek(self.range_start)
    remaining = 1 + self.range_end - self.range_start
    while remaining > 0:
      read_buffer = source.read(min(FILE_COPY_BUFFER_SIZE, remaining))
      if not read_buffer:
        return
      try:
        outputfile.write(read_buffer)
      except (ConnectionResetError, BrokenPipeError):
        return False
      remaining -= len(read_buffer)
  
  def _GetRange(self):
    """Parses the Range header from the request, if any.
  
    Returns:
      (Optional[int], Optional[int]), the start and end of the specifed byte
          range. Either may be None if not specified in the request.
    """
    range_header = self.headers["Range"]
    if not range_header:
      return (None, None)
    match = re_compile(r"^bytes=(\d+)\-(\d+)?").search(range_header)
    if not match:
      return (None, None)
    if match.group(2) is not None:
      return (int(match.group(1)), int(match.group(2)))
    else:
      return (int(match.group(1)), None)
  
  def _SendRegularHeaders(self, file_stat):
    """ Sends headers for a regular (non-range) response. """
    ctype = self.guess_type(global_single_file)
    self.send_response(200)
    self.send_header("Content-type", ctype)
    self.send_header("Content-Length", str(file_stat[6]))
    self.send_header("Last-Modified", self.date_time_string(file_stat.st_mtime))
    self.end_headers()
  
  def _SendRangeHeaders(self, file_stat):
    """Sends headers for a range response."""
    ctype = self.guess_type(global_single_file)
    self.send_response(206)
    self.send_header("Content-type", ctype)
    file_size = file_stat[6]
    if self.range_end is None or self.range_end >= file_size:
      self.range_end = file_size - 1
    self.send_header("Content-Range",
                     f"bytes {self.range_start}-{self.range_end}/{file_size}")
    self.send_header("Content-Length",
      str(1 + self.range_end - self.range_start))
    self.send_header("Last-Modified", self.date_time_string(file_stat.st_mtime))
    self.end_headers()

class ThreadedHTTPServer(Thread):
  """ Not to be confused with ThreadingHTTPServer as this is a non-blocking
      server that can be stopped with the method close()
  """
  def __init__(self, server_address, request_handler_obj, bind_n_act=True):
    self.httpd = HTTPServer(
      server_address, request_handler_obj, bind_n_act)
    self.poll_interval = .5
    super(ThreadedHTTPServer, self).__init__()
    self.httpd.log_message = lambda *_: 0
    self.httpd.log_request = lambda *_: 0
  
  def run(self):
    self.httpd.serve_forever(self.poll_interval)
  
  def terminate(self):
    self.httpd.shutdown()

def get_cast(friendly_name: str) -> Chromecast:
  """ Finds the cast device on the local network with the specified name.
      Raises:
        ValueError: No cast device with the friendly name could be found.
  """
  try:
    chromecasts, browser = get_chromecasts()
    stop_discovery(browser)
    for cast in chromecasts:
      if cast.device.friendly_name.lower() == friendly_name.lower():
        return cast
    raise ValueError(
      f"Couldn't find device, options are: {[c.device.friendly_name for c in chromecasts]}")
  except AssertionError: # fixes a zeroconf error on rpi, by using recursion
    sleep(1)
    return get_cast(friendly_name)

def play_media(
    port: int, media_controller: MediaController, filename: str, title: str):
  """ Starts media playback on a cast device. """
  sock = socket(AF_INET, SOCK_DGRAM)
  sock.connect(("1.1.1.1", 80))
  media_controller.play_media(
    f"http://{sock.getsockname()[0]}:{port}/file", guess_type(filename)[0], title=title)
  sock.close()
  media_controller.block_until_active()
  return media_controller

def interactive(total: int, title: str, caster: Chromecast, httpd: ThreadedHTTPServer):
  print(end=f"\x1B[2J {title} should be playing on the {caster.device.friendly_name}")
  print(" Controls ⌨️ :")
  print(" ⬆️ ⬇️  to control volume", "⬅️ ➡️  to control seek",
    "🅿️  or space for ▶️  or ⏸️", "🆀  to quit casting", "🅼  to mute 🔇️",
    sep="\n ", end="\n\n")
  
  controller = caster.media_controller
  
  while controller.status.player_is_idle:
    sleep(.5)
    controller.update_status()
  start = time()
  
  from termios import tcgetattr, tcsetattr
  from tty import setraw, setcbreak
  from os import get_terminal_size, read
  from sys import stdin
  
  from select import select
  
  def stftime(time_seconds):
    minute, second = divmod(time_seconds, 60)
    hour, minute = divmod(minute, 60)
    if hour > 0:
      return 8, f"{int(hour):02}:{int(minute):02}:{int(second):02}"
    else:
      return 5, f"{int(minute):02}:{int(second):02}"
  
  def progress_bar(current, total):
    size, tm = stftime(current)
    width = (int(get_terminal_size().columns) - size + 2) * 8
    progress, index = divmod((current if current else 1)/total * width, 8)
    print(f"\x1b[2K {tm} {'█' * int(progress)}{'▏▎▍▌▋▊▉█'[int(index)]}",
      end='\r')
  
  def getch():
    if select([stdin], [], [], 0)[0] == [stdin]:
      char = stdin.read(1)
      if char == '\x1B':
        char = stdin.read(2)
      return char
    return ''
  
  running = True
  
  stdin.flush()
  attributes = tcgetattr(stdin)
  setraw(stdin)
  
  def on_exit():
    nonlocal running, attributes, caster, httpd
    caster.media_controller.stop()
    caster.quit_app()
    httpd.terminate()
    httpd.join()
    running = False
    tcsetattr(stdin, 2, attributes)
    print(end="\x1B[2J")
  
  caster.media_controller.channel_disconnected = on_exit
  current = controller.status.current_time
  
  while not controller.status.player_is_idle and running:
    char = getch()
    delay = time() - start
    if delay > 1 or char:
      try:
        caster.media_controller.update_status()
      except pychromecast.error.NotConnected:
        sleep(.1)
      current = current + delay
      start = time()
      progress_bar(current, total)
      if char == '[A': # Up
        caster.volume_up(.01)
      elif char == '[B': # Down
        caster.volume_down(.01)
      elif char == '[D' or char == '[C': # Left or Right
        i = 30 if char == '[C' else -30
        current = min(max(current + i, 0), total)
        controller.seek(current)
        start = time()
      elif ('p' in char) or (' ' in char):
        controller.pause()
        char = getch()
        while not (('p' in char) or (' ' in char)):
          char = getch()
        controller.play()
        start = time()
      elif 'm' in char:
        caster.volume_down(1)
      elif 'q' in char: # Quit
        controller.pause()
        print(end="\x1B[2K Are you sure you want to quit? (Y/n)\r")
        char = getch()
        while not char:
          char = getch()
        if char != 'n':
          break
        controller.play()
      current = caster.media_controller.status.current_time
  return on_exit()


def onetime(caster, httpd):
  while caster.media_controller.status.player_is_idle:
    sleep(.1)
    caster.media_controller.update_status()
  
  running = True
  
  def on_exit():
    nonlocal running
    caster.media_controller.stop()
    caster.quit_app()
    httpd.terminate()
    httpd.join()
    running = False
  
  caster.media_controller.channel_disconnected = on_exit
  while not caster.media_controller.status.player_is_idle and running:
    sleep(1)
    caster.media_controller.update_status()
  
  return on_exit()

if __name__ == "__main__":
  parser = ArgumentParser(
    prog="simeplcast",
    usage="simplecast --device 'Living Room TV' --title 'Film Title' film.mp4",
    description="Cast media.")
  parser.add_argument(
    "--device", type=str, help="The friendly name of the device to cast to.")
  parser.add_argument(
    "--port", type=int, default=8080,
    help="The port for your computer to host the content on.")
  parser.add_argument(
    "--title", type=str, default="Simplecast Media",
    help="Title of media your casting")
  parser.add_argument(
    "--process", type=str, default="onetime",
    help="Is the process interactive or onetime use & on linux")
  parser.add_argument(
    "filename", metavar="FILENAME", type=str,
    help="Filename for file to cast")
  
  args = parser.parse_args()
  
  # canonicalizes a file path
  global_single_file = abspath(expanduser(args.filename))
  
  caster = get_cast(args.device)
  caster.wait()
  
  httpd = ThreadedHTTPServer(('', args.port), SingleFileHTTPRequestHandler)
  httpd.start()
  # Sleep briefly while the server thread starts up.
  sleep(2)
  media_controller = play_media(
    args.port, caster.media_controller, args.filename, args.title)
  sleep(1)
  
  if args.process == "interactive":
    interactive(
      int(float(mediainfo(args.filename)["duration"])), # <-- total time
      args.title, caster, httpd)
  else:
    onetime(caster, httpd)
