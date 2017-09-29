#!/usr/bin/env python3

"""A dead-simple script to cast media files to ChromeCasts and compatibles.
"""

import argparse
import http.server
import mimetypes
import os
import os.path
import socket
import threading
import time

import pychromecast.pychromecast as pychromecast


# A global variable containing the path of the single file to be served via
# HTTP.
global_single_file = None


class SingleFileHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
  """HTTP request handler that only serves a single file.
  """

  def send_head(self):
    if self.path != "/file":
      self.send_error(404, "File not found")
      return None
    ctype = self.guess_type(global_single_file)
    try:
      f = open(global_single_file, 'rb')
    except OSError:
      self.send_error(404, "File not found")
      return None
    try:
      self.send_response(200)
      self.send_header("Content-type", ctype)
      fs = os.fstat(f.fileno())
      self.send_header("Content-Length", str(fs[6]))
      self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
      self.end_headers()
      return f
    except:
      f.close()
      raise


class CallableHttpServer(object):
  """Callable object that runs an HTTP server with SingleFileHTTPRequestHandler.
  """

  def __init__(self, port):
    """Prepares object to serve on the specified port.

    Args:
      port: int, The port
    """
    self._port = port

  def __call__(self):
    """Starts HTTP server and runs indefinitely."""
    httpd = http.server.HTTPServer(("", self._port),
                                   SingleFileHTTPRequestHandler)
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
    pychromecast.Chromecast: Object representing the specified cast device.

  Raises:
    ValueError: No cast device with the specifed friendly name could be found.
  """
  chromecasts = pychromecast.get_chromecasts()
  for cast in chromecasts:
    if cast.device.friendly_name == friendly_name:
      return cast
  raise ValueError("Couldn't find device, options are: {}".format(
      [cc.device.friendly_name for cc in chromecasts]))


def PlayMedia(port, media_controller, filename):
  """Starts media playback on a cast device.

  HTTP server must be running when this function is called.

  Args:
    port: int, The port of the HTTP server on this machine.
    media_controller: pychromecast.MediaController, the media controller of the
        target cast device.
    filename: The local filename to play.
  """
  type, _ = mimetypes.guess_type(filename)
  url = "http://{}:{}/file".format(GetIp(), port)
  media_controller.play_media(url, type)
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
                      help="The name of the device to cast to.")
  parser.add_argument("--port", type=int, default=8080,
                      help="The port to serve HTTP content on.")
  parser.add_argument("filename", metavar="FILENAME", type=str,
                      help="The file to cast")
  args = parser.parse_args()

  global global_single_file
  global_single_file = CanonicalizeFilePath(args.filename)

  cast = GetCast(args.device)
  cast.wait()

  callable_http_server = CallableHttpServer(args.port)
  http_server_thread = threading.Thread(target=callable_http_server)
  http_server_thread.start()

  # Sleep briefly while the server thread starts up.
  time.sleep(2)

  PlayMedia(args.port, cast.media_controller, args.filename)
  # http_server_thread never actually terminates. For now this script has to be
  # killed.
  http_server_thread.join()


if __name__ == "__main__":
  main()
