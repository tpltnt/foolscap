
import os.path, time, pickle, bz2
from zope.interface import implements
from twisted.internet import reactor
from foolscap.logging.interfaces import IIncidentReporter
from foolscap.logging import levels
from foolscap.eventual import eventually
from foolscap import base32

class IncidentQualifier:
    """I am responsible for deciding what qualifies as an Incident. I look at
    the event stream and watch for a 'triggering event', then signal my
    handler when the events that I've seen are severe enought to warrant
    recording the recent history in an 'incident log file'.

    My event() method should be called with each event. When I declare an
    incident, I will call my handler's declare_incident(ev) method, with the
    triggering event. Since event() will be fired from an eventual-send
    queue, the incident will be declared slightly later than the triggering
    event.
    """

    def set_handler(self, handler):
        self.handler = handler

    def check_event(self, ev):
        if ev['level'] >= levels.WEIRD:
            return True
        return False

    def event(self, ev):
        if self.check_event(ev) and self.handler:
            self.handler.declare_incident(ev)

class IncidentReporter:
    """Once an Incident has been declared, I am responsible for making a
    durable record all relevant log events. I do this by creating a logfile
    (a pickle of log event dictionaries) and copying everything from the
    history buffer into it. I can copy a small number of future events into
    it as well, to record what happens as the application copes with the
    situtation.

    I am responsible for just a single incident.

    I am created with a reference to a FoolscapLogger instance, from which I
    will grab the contents of the history buffer.

    When I have closed the incident logfile, I will notify the logger by
    calling their incident_recorded(filename) method, passing it the filename
    of the logfile I created. This can be used to notify remote subscribers
    about the incident that just occurred.
    """
    implements(IIncidentReporter)

    TRAILING_DELAY = 5.0 # gather 5 seconds of post-trigger events
    TRAILING_EVENT_LIMIT = 100 # or 100 events, whichever comes first
    TIME_FORMAT = "%Y-%m-%d-%H%M%S"

    def __init__(self, basedir, logger, tubid_s):
        self.basedir = basedir
        self.logger = logger
        self.tubid_s = tubid_s

    def format_time(self, when):
        return time.strftime(self.TIME_FORMAT, time.localtime(when))

    def incident_declared(self, triggering_event):
        # choose a name for the logfile
        now = time.time()
        unique = os.urandom(4)
        unique_s = base32.encode(unique)
        filename = "incident-%s-%s.flog" % (self.format_time(now),
                                            unique_s)
        self.abs_filename = os.path.join(self.basedir, filename)
        self.abs_filename_bz2 = self.abs_filename + ".bz2"
        # open logfile. We use both an uncompressed one and a compressed one.
        self.f1 = open(self.abs_filename, "wb")
        self.f2 = bz2.BZ2File(self.abs_filename_bz2, "wb")
        # write header with triggering_event
        header = {"header": {"type": "incident",
                             "trigger": triggering_event,
                             }}
        pickle.dump(header, self.f1)
        pickle.dump(header, self.f2)

        # subscribe to events that occur after this one
        self.still_recording = True
        self.remaining_events = self.TRAILING_EVENT_LIMIT
        self.logger.addObserver(self.trailing_event)

        # use self.logger.buffers, copy events into logfile
        events = list(self.logger.get_buffered_events())
        events.sort(lambda a,b: cmp(a['num'], b['num']))
        for e in events:
            wrapper = {"from": self.tubid_s,
                       "rx_time": now,
                       "d": e}
            pickle.dump(wrapper, self.f1)
            pickle.dump(wrapper, self.f2)

        self.f1.flush()
        # the BZ2File has no flush method

        # now we wait for the trailing events to arrive

        self.timer = reactor.callLater(self.TRAILING_DELAY,
                                       self.stop_recording)

    def trailing_event(self, ev):
        if not self.still_recording:
            return

        self.remaining_events -= 1
        if self.remaining_events >= 0:
            wrapper = {"from": self.tubid_s,
                       "rx_time": time.time(),
                       "d": ev}
            pickle.dump(wrapper, self.f1)
            pickle.dump(wrapper, self.f2)
            return

        self.stop_recording()

    def stop_recording(self):
        self.still_recording = False
        if self.timer.active():
            self.timer.cancel()

        self.logger.removeObserver(self.trailing_event)
        # Observers are notified through an eventually() call, so we might
        # get a few more after the observer is removed. We use
        # self.still_recording to hush them.
        eventually(self.finished_recording)

    def finished_recording(self):
        self.f2.close()
        # the compressed logfile has closed successfully. We no longer care
        # about the uncompressed one.
        self.f1.close()
        os.unlink(self.abs_filename)

        # now we can tell the world about our new incident record
        eventually(self.logger.incident_recorded, self.abs_filename_bz2)

