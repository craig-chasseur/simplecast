#!/usr/bin/env python3

"""A dead-simple script to cast media files to ChromeCasts and compatibles.
"""

import argparse
import http.server
import mimetypes
import os
import os.path
import re
import socket
import threading
import time

import pychromecast.pychromecast as pychromecast

DEFAULT_SUBTITLES_MIME_TYPE = "text/vtt"
FILE_COPY_BUFFER_SIZE = 64 * 1024


# Global variables containing the paths of files to be served via HTTP.
global_video_file = None
global_subtitles_file = None
global_subtitles_mime_type = DEFAULT_SUBTITLES_MIME_TYPE


class CastHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
  """HTTP request handler for casting.

  Supports serving a single video file and an optional subtitles file. Supports
  range and cross-origin resource sharing (CORS).
  """

  def do_HEAD(self):
    self.range_start, self.range_end = self._GetRange()
    f = self.send_head()
    if f:
      f.close()

  def do_GET(self):
    self.range_start, self.range_end = self._GetRange()
    f = self.send_head()
    if f:
      try:
        if self.range_start is None:
          self.copyfile(f, self.wfile)
        else:
          self.copy_range(f, self.wfile)
      except ConnectionResetError:
        # ConnectionResetError is normal when ChromeCast stops or seeks.
        pass
      finally:
        f.close()

  def send_head(self):
    """Sends header common to HEAD and GET requests.

    Returns:
      file, an open file containing the content to serve.
    """
    file_path = None
    mime_type = None
    if self.path == "/video":
      file_path = global_video_file
    elif self.path == "/subtitles":
      file_path = global_subtitles_file
      mime_type = global_subtitles_mime_type
    else:
      self.send_error(404, "File not found")
      return None

    try:
      f = open(file_path, 'rb')
    except OSError:
      self.send_error(404, "File not found")
      return None

    try:
      file_stat = os.fstat(f.fileno())
      if self.range_start is None:
        self._SendRegularHeaders(file_path, file_stat, mime_type)
      else:
        self._SendRangeHeaders(file_path, file_stat, mime_type)
    except:
      f.close()
      raise

    return f

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
      outputfile.write(read_buffer)
      remaining -= len(read_buffer)

  def end_headers(self):
    self.send_header("Accept-Ranges", "bytes")
    self.send_header("Access-Control-Allow-Origin", "*")
    return super().end_headers()

  def _GetRange(self):
    """Parses the Range header from the request, if any.

    Returns:
      (Optional[int], Optional[int]), the start and end of the specifed byte
          range. Either may be None if not specified in the request.
    """
    range_header = self.headers["Range"]
    if not range_header:
      return (None, None)
    bytes_regex = re.compile(r"^bytes=(\d+)\-(\d+)?")
    match = bytes_regex.search(range_header)
    if not match:
      return (None, None)
    if match.group(2) is not None:
      return (int(match.group(1)), int(match.group(2)))
    else:
      return (int(match.group(1)), None)

  def _SendRegularHeaders(self, file_path, file_stat, mime_type):
    """Sends headers for a regular (non-range) response."""
    if not mime_type:
      mime_type = self.guess_type(file_path)
    self.send_response(200)
    self.send_header("Content-type", mime_type)
    self.send_header("Content-Length", str(file_stat.st_size))
    self.send_header("Last-Modified", self.date_time_string(file_stat.st_mtime))
    self.end_headers()

  def _SendRangeHeaders(self, file_path, file_stat, mime_type):
    """Sends headers for a range response."""
    if not mime_type:
      mime_type = self.guess_type(file_path)
    self.send_response(206)
    self.send_header("Content-type", mime_type)
    file_size = file_stat[6]
    if self.range_end is None or self.range_end >= file_size:
      self.range_end = file_size - 1
    self.send_header("Content-Range",
                     "bytes {}-{}/{}".format(self.range_start, self.range_end,
                                       file_size))
    self.send_header("Content-Length",
                     str(1 + self.range_end - self.range_start))
    self.send_header("Last-Modified", self.date_time_string(file_stat.st_mtime))
    self.end_headers()


class CallableHttpServer(object):
  """Callable object that runs an HTTP server with CastHTTPRequestHandler.
  """

  def __init__(self, port):
    """Prepares object to serve on the specified port.

    Args:
      port: int, The port
    """
    self._port = port

  def __call__(self):
    """Starts HTTP server and runs indefinitely."""
    httpd = http.server.ThreadingHTTPServer(("", self._port),
                                            CastHTTPRequestHandler)
    httpd.serve_forever()


def GetIp():
  """Returns this machine's external IP address.

  Returns:
    str: This machine's external IP address.
  """
  s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  s.connect(("8.8.8.8", 80))
  return s.getsockname()[0]


def GetCast(friendly_name):
  """Finds the cast device on the local network with the specified name.

  Args:
    friendly_name: str, The friendly name of the cast device to look up.

  Returns:
    Tuple[pychromecast.Chromecast, pychromecast.CastBrowser]: Object
        representing the specified cast device and a service browser that keeps
        ChromeCast mDNS data updated

  Raises:
    ValueError: No cast device with the specifed friendly name could be found.
  """
  chromecasts, browser = pychromecast.get_chromecasts()
  for cast in chromecasts:
    if cast.device.friendly_name == friendly_name:
      return (cast, browser)
  raise ValueError("Couldn't find device, options are: {}".format(
      [cc.device.friendly_name for cc in chromecasts]))


def PlayMedia(port, media_controller, filename, has_subtitles):
  """Starts media playback on a cast device.

  HTTP server must be running when this function is called.

  Args:
    port: int, The port of the HTTP server on this machine.
    media_controller: pychromecast.MediaController, the media controller of the
        target cast device.
    filename: str, The local video filename to play.
    has_subtitles: bool, Whether there is a subtitles track.
  """
  videotype, _ = mimetypes.guess_type(filename)

  ip = GetIp()
  url = "http://{}:{}/video".format(ip, port)
  suburl = "http://{}:{}/subtitles".format(ip, port) if has_subtitles else None
  media_controller.play_media(url, videotype, subtitles=suburl,
                              subtitles_mime=global_subtitles_mime_type)
  media_controller.block_until_active()


def CanonicalizeFilePath(path):
  """Canonicalizes a file path, expanding user token and converting to absolute.

  Args:
    path: str, The path to canonicalize.

  Returns:
    str, The canonicalized form of path.
  """
  return os.path.abspath(os.path.expanduser(path))


def main():
  """Main function.
  """
  parser = argparse.ArgumentParser(description="Cast media.")
  parser.add_argument("--device", type=str,
                      help="The name of the device to cast to")
  parser.add_argument("--port", type=int, default=8080,
                      help="The port to serve HTTP content on")
  parser.add_argument("--subtitles_file", type=str,
                      help="Optional subtitles file")
  parser.add_argument("--subtitles_mime_type", type=str,
                      default=DEFAULT_SUBTITLES_MIME_TYPE,
                      help="MIME type of subtitles")
  parser.add_argument("filename", metavar="FILENAME", type=str,
                      help="The file to cast")
  args = parser.parse_args()

  global global_video_file
  global_video_file = CanonicalizeFilePath(args.filename)

  global global_subtitles_file
  global global_subtitles_mime_type
  if args.subtitles_file:
    global_subtitles_file = CanonicalizeFilePath(args.subtitles_file)
    global_subtitles_mime_type = args.subtitles_mime_type

  cast, browser = GetCast(args.device)
  cast.wait()

  callable_http_server = CallableHttpServer(args.port)
  http_server_thread = threading.Thread(target=callable_http_server,
                                        daemon=True)
  http_server_thread.start()

  # Sleep briefly while the server thread starts up.
  time.sleep(2)

  PlayMedia(args.port, cast.media_controller, args.filename,
            args.subtitles_file is not None)

  # Now that playback has started we can stop the browser.
  browser.stop_discovery()

  # http_server_thread never actually terminates. For now this script has to be
  # killed.
  http_server_thread.join()


if __name__ == "__main__":
  main()
