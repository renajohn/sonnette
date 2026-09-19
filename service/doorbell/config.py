from dataclasses import dataclass
from pathlib import Path
import yaml

from .driver import Driver

_UNREACHABLE = ("localhost", "127.0.0.1", "::1")

@dataclass(frozen=True)
class Config:
    broker_host: str
    broker_port: int
    ring_prefix: str
    pub_prefix: str
    model_url: str
    photos_dir: Path
    data_dir: Path
    active_mode: bool
    stale_s: float = 120.0
    serve_port: int = 8099
    serve_enabled: bool = True
    retention_days: int = 180
    burst_shots: int = 5
    burst_interval_s: float = 11.0
    burst_max_shots: int = 12
    night_start_h: int = 22
    night_end_h: int = 6
    ring_silence_s: float = 960.0

    @classmethod
    def load(cls, path: Path) -> "Config":
        d = yaml.safe_load(Path(path).read_text())
        for value in (d["broker_host"], d["model_url"]):
            if any(h in str(value) for h in _UNREACHABLE):
                raise ValueError(
                    "Container is not on the host network: use the host IP, "
                    f"not {value!r}"
                )
        # Checked on the CLEANED value, the one actually used everywhere
        # else below (an earlier version compared d["pub_prefix"] raw here,
        # so "ring/" rstrip("/")'d down to "ring" -- which does not start
        # with "ring/" -- while the raw comparison "ring/" == "ring" was
        # also false: "ring/" and "ring//" slipped through as pub_prefix
        # "ring", and the service would have published its heartbeat, its
        # discovery and its ghost-visit closures onto ring/# itself).
        cleaned_pub_prefix = d["pub_prefix"].rstrip("/")
        if cleaned_pub_prefix == "ring" or cleaned_pub_prefix.startswith("ring/"):
            # The single-driver rule ("never publish under ring/#") is only
            # mechanically enforced inside pipeline._emit's assert --
            # __main__ also calls bus.publish directly in four other places
            # (heartbeat, close_ghost_visits, discovery, tick's events),
            # none of which go through that assert. Refusing a pub_prefix
            # that itself starts with "ring/" here makes the rule structural
            # instead of partial: it is correct in production
            # (doorbell/porch), but nothing else stopped a future
            # config.yaml from breaking it.
            raise ValueError(
                "pub_prefix must not start with 'ring/': this service must "
                f"never publish onto the topic tree it listens on, got {d['pub_prefix']!r}"
            )
        retention_days = int(d.get("retention_days", 180))
        if retention_days < 32:
            # Same failure mode as the localhost check above: fail loudly at
            # startup rather than silently corrupt the archive on the first
            # retention pass. retention_days: 0 or a negative value would
            # delete most or all of the archive outright, and a very small
            # value is the same risk in a milder form -- the same concern
            # purge_old_photos's own circuit breaker exists for.
            #
            # 32 is kept as that sane minimum, not (any longer) as the thing
            # that closes the race between the retention purge and a
            # concurrent store.write(): an earlier version of this comment
            # claimed a monthly folder can hold at most 31 days, so the
            # *current* month's own folder could never contain a file old
            # enough to purge -- true, but beside the point once you notice
            # that store.write() names a photo's folder from Ring's own
            # timestamp, not from wall-clock now (measured: a 200-day-stale
            # timestamp lands in a folder six months in the past). Any
            # month's folder can receive a write, so this floor alone does
            # not bound which folder purge_old_photos might race. What
            # actually closes that race now is purge_old_photos() never
            # rmdir()'ing the current or previous calendar month's folder,
            # unconditionally -- see its own docstring for the full
            # argument and the residual risk it still names honestly.
            raise ValueError(
                "retention_days must be at least 32 -- smaller values risk "
                "deleting most or all of the archive in one pass, "
                f"got {retention_days!r}"
            )
        burst = (int(d.get("burst_shots", 5)),
                 float(d.get("burst_interval_s", 11.0)),
                 int(d.get("burst_max_shots", 12)))
        # Driver's own constructor is the single statement of what a valid
        # burst is; building one here makes a bad value fail at startup, in
        # passive mode too, instead of on the day active_mode is switched on.
        Driver(*burst)
        night_start_h = int(d.get("night_start_h", 22))
        night_end_h = int(d.get("night_end_h", 6))
        if not (0 <= night_end_h < night_start_h <= 23):
            raise ValueError(
                "need 0 <= night_end_h < night_start_h <= 23 (a night that "
                f"spans midnight), got {night_start_h!r} and {night_end_h!r}")
        return cls(
            broker_host=d["broker_host"],
            broker_port=int(d["broker_port"]),
            ring_prefix=d["ring_prefix"].rstrip("/"),
            pub_prefix=cleaned_pub_prefix,
            model_url=d["model_url"],
            photos_dir=Path(d["photos_dir"]),
            data_dir=Path(d["data_dir"]),
            active_mode=bool(d["active_mode"]),
            # d.get, never d[...]: production's config.yaml carries only 8
            # keys, and requiring this 9th one would stop the service from
            # starting on the next redeploy.
            stale_s=float(d.get("stale_s", 120.0)),
            # Same reasoning for these three: none of them may become
            # required, or the next redeploy of the unchanged production
            # config.yaml would refuse to start the service.
            serve_port=int(d.get("serve_port", 8099)),
            serve_enabled=bool(d.get("serve_enabled", True)),
            retention_days=retention_days,
            burst_shots=burst[0],
            burst_interval_s=burst[1],
            burst_max_shots=burst[2],
            night_start_h=night_start_h,
            night_end_h=night_end_h,
            ring_silence_s=float(d.get("ring_silence_s", 960.0)),
        )
