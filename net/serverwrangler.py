import gobject
import glib
import select
import Queue
import threading
import random

from net.server import DHTServer
from net.sql import SQLiteThread
from net.torrent import TorrentDB
from net.upnp import UPNPManager
from net.contactinfo import ContactInfo
from net.sha1hash import Hash


class ServerWrangler(gobject.GObject):
  incoming = gobject.property(type=bool, default=False)
  __gsignals__ = {
    "server-added": (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
      (DHTServer,)),
    "upnp-error": (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
      (gobject.TYPE_PYOBJECT, str))
  }
  timeout = 100
  def __init__(self, config, logfunc=None):
    gobject.GObject.__init__(self)

    self.running = False
    self.logfunc = logfunc
    self.config = config
    self.servers = []
    self.pending = Queue.Queue()
    self.thread = None

    self.upnp = UPNPManager()
    self.upnp.connect("port-added", self._port_added)
    self.upnp.connect("add-port-error", self._add_port_error)

    self.conn = SQLiteThread(self.config.get("torrent", "db"))
    self.conn.start()
    self.conn.executescript(open("sql/db.sql","r").read())

    self.torrents = TorrentDB(self.conn, self._log)

    servers = self.conn.select("SELECT * FROM servers")
    for server in servers:
      self.add_server(server["hash"], server["bind"], server["host"],
                      server["upnp"], False)
  def add_server(self, hash, bind, host, upnp, insert=True):
    if upnp:
      if insert:
        self.conn.execute("""INSERT INTO servers(hash, bind, host, upnp)
                             VALUES (?, ?, ?, ?)""",
                          (hash, bind, None, True))
      self.upnp.add_udp_port(bind)
    else:
      if insert:
        server_id = self.conn.insert("""INSERT INTO servers(hash, bind, host,
                                        upnp) VALUES (?, ?, ?, ?)""",
                                   (hash, bind, host, False))
      else:
        server_id = self.conn.select_one("""SELECT id FROM servers WHERE
                                            hash=?""", (hash,))["id"]
      self._do_add_server(hash, bind, host, server_id)
  def add_servers(self, bind_addr, host_addr, min_port, max_port, upnp,
                  uniform):
    """Generates servers on ports from min_port to max_port inclusive."""
    hashes = []
    if uniform:
      hashes = range(0x0, (1 << 160)-1, ((1 << 160)-1)/(max_port-min_port+1))
    else:
      for i in range(min_port, max_port+1):
        hashes.append(random.getrandbits(160))
    hashes = [Hash(h) for h in hashes]
    for i, port in enumerate(range(min_port, max_port+1)):
      self.add_server(hashes[i], ContactInfo(bind_addr, port),
                      ContactInfo(host_addr, port), upnp)
  def _do_add_server(self, hash, bind, host, id):
    new_server = DHTServer(self.config, id, hash, bind, host,
                           self.conn, self.torrents, self._log)
    new_server.connect("notify::incoming", self._do_notified)
    self.pending.put(new_server)
    glib.idle_add(self.emit, "server-added", new_server)
  def _do_notified(self, server, value):
    self.incoming = (value or self.incoming)
  def _port_added(self, manager, external, internal):
    row = self.conn.select_one("SELECT * FROM servers WHERE bind=?",
                               (internal,))
    self._do_add_server(row["hash"], internal, external, row["id"])
  def _add_port_error(self, manager, internal, error):
    glib.idle_add(self.emit, "upnp-error", internal, error)
  def _log(self, msg):
    if self.logfunc:
      self.logfunc(msg)
  def launch_dispatch(self):
    self.thread = threading.Thread(target=self.dispatch)
    self.thread.daemon  = True
    self.thread.start()
  def dispatch(self):
    self.running = True
    poll = select.poll()
    fds = {}
    while self.running:
      while True:
        try:
          a = self.pending.get(False)
        except Queue.Empty:
          break
        else:
          self.servers.append(a)
          fd = a.fileno()
          fds[fd] = a
          poll.register(fd, select.POLLIN)
      result = poll.poll(self.timeout)
      for (fd, event) in result:
        fds[fd].handle_request()
  def shutdown(self):
    self.running = False
    if self.thread is not None:
      try:
        self.thread.join()
      except:
        pass
    for server in self.servers:
      server.shutdown()
    self.upnp.shutdown()
    self.conn.close()