import hashlib
import json
import queue
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import driver as driving
from . import publisher, webserve
from .archive import ARCHIVE_DIR
from .bus import Bus
from .classifier import Classifier
from .config import Config
from .frames import Capture
from .ledger import Ledger
from .machine import Event, Machine
from .pipeline import Pipeline
from .probe import Log
from .store import DEFAULT_TIMEZONE
from .summary import Summary
from .watch import Watch

PROBE_IMAGE = Path(__file__).parent / "probe.jpg"   # an empty porch, synthetic, ~23 KB


def heartbeat(bus, cfg, clf, log=None):
    data = PROBE_IMAGE.read_bytes()
    c = Capture(data, hashlib.sha256(data).hexdigest(), 0, "motion", False)
    while True:
        try:
            _one_beat(bus, cfg, clf, c, log)
        except Exception:
            # Third thread, same reasoning as background_loop: if it dies the
            # service silently stops proving it can classify, while still
            # looking healthy. Print and carry on.
            traceback.print_exc()
        time.sleep(300)


def _one_beat(bus, cfg, clf, c, log):
    # I-4, reviewed and deliberately left unfixed: this classify() call and
    # background_loop's own drain_classification() share the same Classifier
    # instance, and nothing serialises the two -- a heartbeat probe firing
    # while a real capture is mid-inference doubles up on a model spec §5
    # describes behind a single-worker queue. The Classifier holds no
    # mutable state, so nothing breaks; the cost is latency, on the one
    # criterion the trial is judged by (spec §7: p90 first verdict < 5 s).
    # Measured window: heartbeat runs every 300 s and one call takes at most
    # a few seconds, so the overlap is on the order of ~2 s out of every
    # 300 -- real, but narrow enough that adding a lock (the one thing this
    # service structurally has none of, everywhere else) would be the worse
    # trade. Consigned here rather than fixed: spec §5 and §8 were never
    # reconciled on this point, and this comment is that reconciliation.
    v = clf.classify(c)
    # Spec §8: this heartbeat attests "a chain able to classify", not merely
    # "a process that responded" -- it is the only notification allowed to
    # reach the phone during the trial, precisely so a silent degradation
    # cannot ruin the measurement. "unparseable" means the model answered
    # WITHOUT producing a verdict (wrong model loaded, prose instead of the
    # expected format, a broken update): counting that as healthy would keep
    # this heartbeat green while every real verdict loses its booleans --
    # exactly the silent failure this probe exists to catch. The probe image
    # is an empty porch, whose only correct answer is N,N,N: only "ok" is
    # health.
    model_ok = v.status == "ok"
    # Publish first, log second — same order as the four event hooks. Latency
    # does not matter on a 300s cycle, but a lone exception to the pattern
    # would mislead the next reader about why the other four are ordered
    # this way.
    if model_ok:
        bus.publish(f"{cfg.pub_prefix}/heartbeat",
                    json.dumps({"t": time.time(),
                                "model_ok": True}).encode())
    if log:
        log.health({"t": time.time(), "model_ok": model_ok})


def _current_and_previous_month_labels(now, timezone=DEFAULT_TIMEZONE):
    """The two "%Y-%m" folder labels (store.path_for's own naming) that an
    ordinary, non-stale store.write() can still reach for around `now`: the
    current calendar month, and the previous one to cover the few days
    right at a month boundary. Imports store.DEFAULT_TIMEZONE rather than
    repeating the "Europe/Zurich" literal (N-5, reviewed finding): two
    independently-typed copies of the same string could drift apart
    silently, one shared constant cannot.
    """
    when = datetime.fromtimestamp(now, ZoneInfo(timezone))
    previous = when.replace(day=1) - timedelta(days=1)
    return {f"{when:%Y-%m}", f"{previous:%Y-%m}"}


def purge_old_photos(photos_dir, retention_days, now=None):
    """Delete *.jpg files under photos_dir older than retention_days, then
    remove any monthly folder left empty -- broadly the same two-step idea
    as the purge command this replaces (`find ... -mtime +180 -delete` for
    the files, a second `find` for the directories it leaves empty), though
    not an exact match: `find -mtime +180` truncates age to whole calendar
    days and keeps a file at 180 days and a few hours, while the strict
    second-based comparison below purges it up to a day earlier. That
    direction -- a little more eager, never less -- is the one that matters
    given this project's history of lost photos.

    Strict `<`, not `<=`: a file exactly at the boundary survives. Task 15's
    postmortem is why this is spelled out -- a `>` -> `>=` mutation in an
    earlier guard survived because no test ever pinned the exact boundary
    value, only points strictly on either side of it.

    Never touches a file from an event in progress: the guard only ever
    looks at files older than retention_days (180 by default), and nothing
    from a visit still open is anywhere near that old.

    Scope is deliberately narrow and exact: exactly one directory level
    under photos_dir, and only *.jpg -- the only shape store.py ever
    writes. A *.jpg dropped directly in photos_dir's root, or nested two
    levels down, is never purged; that is accepted as an assumption on
    store.py's own layout, not an oversight, and is pinned by its own test
    rather than left to be discovered by a future change to the glob.

    Never follows a directory symlink: glob("*/*.jpg") would otherwise
    traverse one, stat() would read the *target's* mtime, and unlink()
    would delete the *target*, not the link -- confirmed to destroy the old
    system's archive, which lives in a sibling directory with this exact
    "YYYY-MM/*.jpg" shape. A symlink to a *file* is harmless even without a
    guard (unlink() removes the link, never its target), but the guard
    below covers both anyway, since checking is cheaper than reasoning
    about which case applies. webserve.py makes the same refusal on the
    read side; this is the same refusal on the delete side.

    Circuit breaker: refuses the whole pass, deleting nothing, if it would
    remove more than half of the *.jpg files currently present. This is the
    only defence against a lying clock, because there is no second source
    of time to distinguish "the host clock is ahead" from "these files are
    genuinely old". In steady state a daily pass purges about one day out
    of retention_days -- well under 1% at the default 180 -- so a pass
    wanting to remove over half is not a purge, it is an accident. Refusing
    costs nothing (tomorrow's pass tries again); purging the archive by
    mistake cannot be undone.

    Loud on purpose: the user has already lost photos once to a misplaced
    rm, so nothing here may fail or refuse silently. A missing photos_dir,
    a refused pass, and any file whose deletion fails (an unmounted or
    read-only filesystem, throttled after the first the same way
    Log.write_failures throttles disk errors) all speak on stderr; a normal
    pass that purges something says how many, and a pass with nothing to do
    says nothing at all.

    Returns the number of files purged this pass, for a caller that wants to
    keep a running total.

    No race with a folder store.py just created, closed by construction:
    an earlier version of this function removed every empty directory
    under photos_dir, which could observe a monthly folder in the instant
    between store.py's mkdir() and the first file landing in it, and
    delete a folder store.py was mid-write to -- costing one photo. Given
    that this project has already lost two photos irrecoverably to a
    misplaced rm, and that keeping every photo is the whole point of this
    service (it is why even empty frames are kept, and why task 15's
    freshness guard leaves the photo write unconditional), a rare and
    bounded loss is not an acceptable trade for skipping a lock the service
    otherwise has none of. So this function only ever attempts rmdir() on
    the exact folders it just emptied itself this pass (the parents of the
    files it actually unlinked) -- never on every empty folder under
    photos_dir. A folder with nothing purged from it this pass is never in
    that set, unconditionally: a brand-new, still-empty folder in
    particular can never supply a file old enough to purge, so it can
    never be a candidate for rmdir() at all. The one thing this gives up,
    in the same harmless direction as the find-mtime discrepancy above: a
    folder emptied by something other than this purge (a manual deletion,
    say) is no longer tidied up either -- nobody ever required cleaning up
    someone else's mess, and the failure mode is the one find already
    tolerates, a folder left behind, never a folder deleted twice.

    That does NOT make every race with store.py impossible on its own --
    an earlier, broader claim here was wrong about this, in a way a reviewer
    of the whole branch caught and this paragraph now corrects. The
    argument used to be "a calendar month holds at most 31 days, so the
    *current* month's folder can never hold a file old enough to purge,
    therefore Config.load's floor of 32 keeps it out of reach". True as far
    as it goes, but it answers the wrong question: store.write() names a
    photo's folder from Ring's own timestamp, not from wall-clock now
    (measured: a Ring timestamp 200 days stale lands in a folder six months
    in the past, and timestamp=0 lands in "1970-01"). So the folder a
    concurrent write can still target is not necessarily this month's --
    any month's folder can receive a write if a sufficiently stale
    republication arrives, and retention_days's floor says nothing about
    that. Reproduced with a real FileNotFoundError at retention_days=7,
    where the folder that raced was the current month only because the
    test's capture happened to be recent.

    What actually closes the write race is the exclusion right below:
    photos_dir/<the current calendar month> and .../<the previous one> are
    never rmdir()'d by this function, purged empty or not -- regardless of
    retention_days. Those two labels are the ones store.write() reaches for
    on every ordinary, non-stale capture (the previous one only right at a
    month boundary), so protecting them unconditionally closes the
    realistic path without depending on retention_days at all. Config.load's
    floor of 32 is kept as a sane minimum in its own right (a very small
    value still risks purging most of the archive in one pass, the same
    concern the circuit breaker above exists for) -- but it is no longer the
    thing standing between store.py and this race, and this docstring no
    longer claims it is. The residual, honestly-named risk: a Ring
    timestamp stale enough to name some OTHER, older month, arriving at the
    exact instant that month's folder is being emptied by this pass. No
    such staleness has ever been observed in this project's own archive
    (the worst measured incident was ~7835 s, about 2.2 h -- see _stale's
    docstring). That bound is empirical, not mechanical: nothing in the
    code caps how stale a Ring timestamp can be before it reaches
    store.write(), and _stale itself fails OPEN on timestamp=0 -- the most
    stale case there is -- rather than rejecting it. If a republication
    ever arrived stale enough to name a month outside the protected pair,
    this is where it would show up.
    """
    now = time.time() if now is None else now
    cutoff = now - retention_days * 86400
    root = Path(photos_dir)
    if not root.is_dir():
        print(f"retention: photos_dir {root} is not a directory (unmounted?), "
             "nothing purged", file=sys.stderr, flush=True)
        return 0

    # Never archive/: what the user chose to keep (archive.py) has no
    # retention at all. Its files are hard links to purgeable originals and
    # share their mtime, so nothing but this name keeps them alive.
    candidates = [jpg for jpg in root.glob("*/*.jpg")
                  if jpg.parent.name != ARCHIVE_DIR
                  and not (jpg.is_symlink() or jpg.parent.is_symlink())]
    to_delete = []
    for jpg in candidates:
        try:
            if jpg.stat().st_mtime < cutoff:
                to_delete.append(jpg)
        except OSError:
            continue

    if candidates and len(to_delete) > len(candidates) / 2:
        print(f"retention: refusing to purge {len(to_delete)}/{len(candidates)} "
             "photo(s) in a single pass (over half of the archive) -- this "
             "looks like a wrong retention_days or a clock far in the "
             "future rather than a real retention boundary; nothing was "
             "deleted", file=sys.stderr, flush=True)
        return 0

    purged = 0
    failures = 0
    emptied_parents = set()
    for jpg in to_delete:
        try:
            jpg.unlink()
            purged += 1
            emptied_parents.add(jpg.parent)
        except OSError as exc:
            failures += 1
            if failures == 1 or failures % 100 == 0:
                print(f"retention: deletion failed ({failures}): {exc}",
                     file=sys.stderr, flush=True)

    # Never the current or previous calendar month's own folder (spec I-2
    # follow-up, see the docstring): those are the two labels store.write()
    # can still reach for on an ordinary write, regardless of retention_days,
    # so removing either -- even purged empty -- could race a concurrent
    # store.write() into recreating it. protected is computed fresh from
    # `now`, the same `now` used for the cutoff above, so a long-running
    # process crossing a month boundary mid-pass cannot fall out of sync
    # with it.
    protected = _current_and_previous_month_labels(now)

    # Only the folders this pass itself just emptied -- never every empty
    # folder under photos_dir. See the docstring for why this is what
    # closes the race with store.py's own writes, rather than merely
    # bounding it.
    for folder in emptied_parents:
        if folder.name in protected:
            continue
        if folder.is_symlink():
            # Unreachable by construction, not merely unlikely: every
            # member of emptied_parents is jpg.parent for some jpg this
            # pass actually unlinked, and jpg only ever came from
            # `candidates` above, which already excludes anything whose
            # parent is a symlink. So a symlinked folder can never be
            # added to this set in the first place. Kept anyway as a
            # zero-cost belt for a future change to this function's
            # control flow -- and because rmdir() on a symlink to a
            # directory would raise ENOTDIR rather than silently remove
            # the link or its target, this branch would be a no-op even
            # if it were somehow reached.
            continue
        try:
            folder.rmdir()   # no-op unless now empty
        except OSError:
            pass

    if purged:
        print(f"retention: purged {purged} photo(s) older than "
             f"{retention_days} days", file=sys.stderr, flush=True)
    return purged


def _one_retention_pass(cfg, purged_total, now=None):
    """One daily retention pass: purge, then report the running total on
    stderr whenever something was purged. Without this, purged_total was
    only ever accumulated in memory and never signalled anywhere -- the
    same dead-counter shape task 15 had already fixed once for
    pipeline.stale_frames.
    """
    n = purge_old_photos(cfg.photos_dir, cfg.retention_days, now=now)
    if n:
        purged_total += n
        print(f"retention: {purged_total} photo(s) purged in total since startup",
             file=sys.stderr, flush=True)
    return purged_total


def retention_loop(cfg):
    """Daily retention pass, guarded like the other three threads and for the
    same reason: an unguarded exception here must not silently stop
    enforcing retention while the container still reports healthy -- the
    archive would then grow without bound and nobody would know.
    """
    purged_total = 0
    while True:
        try:
            purged_total = _one_retention_pass(cfg, purged_total)
        except Exception:
            traceback.print_exc()
        time.sleep(86400)


def close_ghost_visits(bus, cfg, log=None):
    """Neither motion/state nor ding/state is retained: there is no way to know
    whether a visit is in progress. Close it, restart from IDLE.

    Goes through msg_event rather than hand-building the payload: the shape of a
    visit_ended must be defined in exactly one place. id and subjects_seen are
    supplied even though they are empty, so that a consumer reading them does
    not hit a KeyError on the one event it sees most rarely.
    """
    ev = Event("visit_ended", {"id": None, "t": time.time(),
                               "end_reason": "restart", "subjects_seen": [],
                               "dings": 0, "photos": []})
    for topic, payload, retain in publisher.msg_event(cfg.pub_prefix, ev):
        bus.publish(topic, payload, retain)
    if log:
        log.event({"event_type": ev.type} | ev.data)


def _one_pass(pipeline, incoming, cfg, bus, probe_log, summary=None):
    """One iteration of background_loop: drain the whole incoming queue,
    classify what was absorbed, then let the state machine's own clock-driven
    transitions (timeouts, abandons) run. Extracted from background_loop so
    it can be exercised directly, the same way heartbeat's _one_beat is --
    background_loop's own `while True` is not unit-tested, by the same
    precedent as heartbeat and retention_loop.
    """
    try:
        frame = incoming.get(timeout=1.0)
    except queue.Empty:
        frame = None
    while frame is not None:
        pipeline.absorb(frame)
        try:
            frame = incoming.get_nowait()
        except queue.Empty:
            frame = None
    pipeline.drain_classification()
    for ev in pipeline.machine.tick(time.time()):
        for m in publisher.msg_event(cfg.pub_prefix, ev):
            bus.publish(*m)
        if probe_log:
            probe_log.event({"event_type": ev.type} | ev.data)
    now = time.time()
    if pipeline.driver and pipeline.driver.tick(
            now, pipeline.machine.state in ("OPEN", "CLOSING")):
        # The single message this service ever writes under ring/# -- see
        # driver.py. Deliberately NOT through pipeline._emit, whose assert
        # keeps guarding everything else.
        bus.publish(*driving.command(cfg.ring_prefix))
        if probe_log:
            probe_log.event({"event_type": "snapshot_requested", "t": now})
    if pipeline.watch:
        alive = pipeline.watch.tick(now)
        if alive is not None:
            bus.publish(f"{cfg.pub_prefix}/ring_link/state",
                        b"ON" if alive else b"OFF", retain=True)
            if probe_log:
                probe_log.health({"t": now, "ring_link": alive})
    if summary:
        payload = summary.refresh(now)
        if payload is not None:
            bus.publish(f"{cfg.pub_prefix}/summary",
                        json.dumps(payload, ensure_ascii=False).encode(),
                        retain=True)
        payload = summary.refresh_day(now)
        if payload is not None:
            bus.publish(f"{cfg.pub_prefix}/day",
                        json.dumps(payload, ensure_ascii=False).encode(),
                        retain=True)


def background_loop(pipeline, incoming, cfg, bus, probe_log, summary=None):
    # Single owner of the pipeline, the ledger, the assembler and the state
    # machine: no other thread ever touches these, so no lock is needed for
    # them and the ordering among them is deterministic. Not literally
    # "nothing mutable is shared" service-wide, though -- publisher's
    # module-level _unknown_event_types_warned set and probe.Log's
    # write_failures counter are each mutated from more than one thread.
    # Both are benign under the GIL (a duplicate warning, an undercounted
    # failure), which is exactly why they stayed out of this sentence
    # instead of needing a lock of their own -- but the absolute claim
    # here is about the four objects this thread owns exclusively, not
    # about the whole service.
    # The MQTT thread only puts frames on the queue.
    # Absorbing is cheap — it writes the photo and enqueues for
    # classification — so the whole queue is drained before classifying.
    while True:
        try:
            _one_pass(pipeline, incoming, cfg, bus, probe_log, summary)
        except Exception:
            # Last line of defence. This thread owns everything; if it dies
            # the container keeps reporting healthy while the MQTT thread
            # piles frames nobody drains. Never let that happen silently:
            # print and carry on.
            traceback.print_exc()
            time.sleep(1.0)


def main():
    cfg = Config.load(Path(sys.argv[1]))
    reg = Ledger(cfg.data_dir / "ledger.db")
    clf = Classifier(cfg.model_url)
    incoming: queue.Queue = queue.Queue()
    bus = Bus(cfg, on_message=incoming.put)
    # The evidence log lives in cfg.data_dir, next to ledger.db: same
    # already-mounted volume, no extra subfolder.
    probe_log = Log(cfg.data_dir)
    # Active mode (spec §11): the driver exists only behind the flag. In
    # passive mode pipeline.driver is None and no code path can press.
    driver = (driving.Driver(cfg.burst_shots, cfg.burst_interval_s,
                             cfg.burst_max_shots)
              if cfg.active_mode else None)
    summary = Summary(cfg)
    pipeline = Pipeline(cfg, reg, clf, Machine(), publish=bus.publish,
                        log=probe_log, driver=driver,
                        watch=Watch(time.time(), cfg.ring_silence_s),
                        summary=summary)

    threading.Thread(target=background_loop,
                     args=(pipeline, incoming, cfg, bus, probe_log, summary),
                     daemon=True).start()
    threading.Thread(target=heartbeat, args=(bus, cfg, clf, probe_log),
                      daemon=True).start()
    threading.Thread(target=retention_loop, args=(cfg,), daemon=True).start()
    if cfg.serve_enabled:
        # The fifth thread (MQTT, background, heartbeat, retention, this
        # one): read-only, never touches reg/pipeline/machine.
        threading.Thread(target=webserve.serve_forever, args=(cfg,),
                         daemon=True).start()
    close_ghost_visits(bus, cfg, probe_log)
    for m in publisher.discovery(cfg.pub_prefix, "homeassistant"):
        bus.publish(*m)
    bus.start()


if __name__ == "__main__":
    main()
