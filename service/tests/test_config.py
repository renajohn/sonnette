from pathlib import Path
import pytest
from doorbell.config import Config

def test_loads_the_configuration(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text(
        "broker_host: 192.0.2.10\n"
        "broker_port: 1883\n"
        "ring_prefix: ring/LOC/camera/DEV\n"
        "pub_prefix: doorbell/porch\n"
        "model_url: http://192.0.2.10:8002/v1/chat/completions\n"
        "photos_dir: /photos\n"
        "data_dir: /data\n"
        "active_mode: false\n"
    )
    c = Config.load(f)
    assert c.broker_host == "192.0.2.10"
    assert c.active_mode is False
    assert c.photos_dir == Path("/photos")

def test_stale_s_defaults_when_absent_from_the_file(tmp_path):
    """Production's config.yaml carries only 8 keys: requiring the 9th would
    stop the service from starting on the next redeploy."""
    f = tmp_path / "c.yaml"
    f.write_text(
        "broker_host: 192.0.2.10\n"
        "broker_port: 1883\n"
        "ring_prefix: ring/LOC/camera/DEV\n"
        "pub_prefix: doorbell/porch\n"
        "model_url: http://192.0.2.10:8002/v1/chat/completions\n"
        "photos_dir: /photos\n"
        "data_dir: /data\n"
        "active_mode: false\n"
    )
    c = Config.load(f)
    assert c.stale_s == 120.0

def test_stale_s_is_configurable(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text(
        "broker_host: 192.0.2.10\n"
        "broker_port: 1883\n"
        "ring_prefix: ring/LOC/camera/DEV\n"
        "pub_prefix: doorbell/porch\n"
        "model_url: http://192.0.2.10:8002/v1/chat/completions\n"
        "photos_dir: /photos\n"
        "data_dir: /data\n"
        "active_mode: false\n"
        "stale_s: 60\n"
    )
    c = Config.load(f)
    assert c.stale_s == 60.0

def test_serve_port_and_serve_enabled_and_retention_days_default_when_absent(tmp_path):
    """Production's config.yaml carries only 8 keys: requiring any of these
    three would stop the service from starting on the next redeploy."""
    f = tmp_path / "c.yaml"
    f.write_text(
        "broker_host: 192.0.2.10\n"
        "broker_port: 1883\n"
        "ring_prefix: ring/LOC/camera/DEV\n"
        "pub_prefix: doorbell/porch\n"
        "model_url: http://192.0.2.10:8002/v1/chat/completions\n"
        "photos_dir: /photos\n"
        "data_dir: /data\n"
        "active_mode: false\n"
    )
    c = Config.load(f)
    assert c.serve_port == 8099
    assert c.serve_enabled is True
    assert c.retention_days == 180

def test_serve_port_and_serve_enabled_and_retention_days_are_configurable(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text(
        "broker_host: 192.0.2.10\n"
        "broker_port: 1883\n"
        "ring_prefix: ring/LOC/camera/DEV\n"
        "pub_prefix: doorbell/porch\n"
        "model_url: http://192.0.2.10:8002/v1/chat/completions\n"
        "photos_dir: /photos\n"
        "data_dir: /data\n"
        "active_mode: false\n"
        "serve_port: 9000\n"
        "serve_enabled: false\n"
        "retention_days: 45\n"
    )
    c = Config.load(f)
    assert c.serve_port == 9000
    assert c.serve_enabled is False
    assert c.retention_days == 45

def _config_yaml(tmp_path, extra=""):
    f = tmp_path / "c.yaml"
    f.write_text(
        "broker_host: 192.0.2.10\n"
        "broker_port: 1883\n"
        "ring_prefix: ring/LOC/camera/DEV\n"
        "pub_prefix: doorbell/porch\n"
        "model_url: http://192.0.2.10:8002/v1/chat/completions\n"
        "photos_dir: /photos\n"
        "data_dir: /data\n"
        "active_mode: false\n"
        + extra
    )
    return f

def test_retention_days_zero_fails_to_load_instead_of_purging_everything(tmp_path):
    """Reviewed finding (I-3): retention_days: 0 reads naturally as "no
    limit" but instead deletes everything except today -- silently, at 3am.
    Refusing at load time, like the localhost check above, is the same
    failure mode as a bad broker_host: fail loudly at startup rather than
    destroy the archive on the first retention pass."""
    f = _config_yaml(tmp_path, "retention_days: 0\n")
    with pytest.raises(ValueError, match="retention_days"):
        Config.load(f)

def test_negative_retention_days_fails_to_load(tmp_path):
    """A negative value would purge the current visit's own photos, not
    just old ones."""
    f = _config_yaml(tmp_path, "retention_days: -1\n")
    with pytest.raises(ValueError, match="retention_days"):
        Config.load(f)

def test_retention_days_31_fails_to_load(tmp_path):
    """Reviewed finding (D-1): a monthly folder can never span more than 31
    days, so at retention_days <= 30 the *current* month's own folder can
    still contain a purgeable file and later become empty mid-write --
    reproduced with a real FileNotFoundError from store.write() at
    retention_days=7. 32 is the smallest value for which that folder can
    never hold a file old enough to purge."""
    f = _config_yaml(tmp_path, "retention_days: 31\n")
    with pytest.raises(ValueError, match="retention_days"):
        Config.load(f)

def test_retention_days_32_is_the_minimum_accepted(tmp_path):
    f = _config_yaml(tmp_path, "retention_days: 32\n")
    assert Config.load(f).retention_days == 32

def test_refuses_a_pub_prefix_under_ring(tmp_path):
    """M-14: the single-driver rule (never publish under ring/#) was only
    ever mechanical inside pipeline._emit's assert -- __main__ calls
    bus.publish directly in four other places (heartbeat, close_ghost_visits,
    discovery, tick's events), none of which go through that assert. Refusing
    a pub_prefix that itself starts with "ring/" at load time makes the rule
    structural instead of partial: correct in production (doorbell/porch),
    but nothing stopped a future config.yaml from breaking it."""
    f = _config_yaml(tmp_path, "")
    f.write_text(f.read_text().replace("pub_prefix: doorbell/porch",
                                       "pub_prefix: ring/porch"))
    with pytest.raises(ValueError, match="pub_prefix"):
        Config.load(f)

def test_refuses_a_pub_prefix_under_ring_with_a_trailing_slash(tmp_path):
    """N-1, reviewed finding: the guard's second disjoint compared the RAW
    value instead of the cleaned one. "ring/" rstrip("/")'s down to "ring",
    which does not start with "ring/", and the raw comparison "ring/" ==
    "ring" is false either -- so "ring/" (and "ring//") slipped through,
    became pub_prefix="ring" after the same rstrip the constructor applies,
    and the service would have published its heartbeat, its discovery and
    its ghost-visit closures onto ring/# -- into the exact tree it listens
    on, through the four direct bus.publish calls in __main__ that
    pipeline._emit's assert never sees."""
    for raw in ("ring/", "ring//"):
        f = _config_yaml(tmp_path, "")
        f.write_text(f.read_text().replace(
            "pub_prefix: doorbell/porch", f"pub_prefix: {raw}"))
        with pytest.raises(ValueError, match="pub_prefix"):
            Config.load(f)

def test_refuses_localhost_for_the_broker(tmp_path):
    """The container is not on the host network: localhost would not reach anything."""
    f = tmp_path / "c.yaml"
    f.write_text(
        "broker_host: localhost\nbroker_port: 1883\n"
        "ring_prefix: r\npub_prefix: p\n"
        "model_url: http://127.0.0.1:8002/v1/chat/completions\n"
        "photos_dir: /photos\ndata_dir: /data\nactive_mode: false\n"
    )
    with pytest.raises(ValueError, match="host network"):
        Config.load(f)


_BASE = (
    "broker_host: 192.0.2.10\n"
    "broker_port: 1883\n"
    "ring_prefix: ring/LOC/camera/DEV\n"
    "pub_prefix: doorbell/porch\n"
    "model_url: http://192.0.2.10:8002/v1/chat/completions\n"
    "photos_dir: /photos\n"
    "data_dir: /data\n"
)


def _load(tmp_path, extra):
    f = tmp_path / "c.yaml"
    f.write_text(_BASE + extra)
    return Config.load(f)


def test_active_mode_loads_with_the_legacy_burst_as_default(tmp_path):
    c = _load(tmp_path, "active_mode: true\n")
    assert c.active_mode is True
    assert (c.burst_shots, c.burst_interval_s, c.burst_max_shots) == (5, 11.0, 12)
    assert (c.night_start_h, c.night_end_h) == (22, 6)


def test_active_mode_does_not_relax_the_pub_prefix_guard(tmp_path):
    """Reopening the single-driver rule for ONE command topic (driver.py)
    is not permission to publish the service's own tree under ring/."""
    f = tmp_path / "c.yaml"
    f.write_text(_BASE.replace("doorbell/porch", "ring/porch") + "active_mode: true\n")
    with pytest.raises(ValueError):
        Config.load(f)


def test_a_burst_interval_ring_mqtt_would_drop_fails_at_startup(tmp_path):
    with pytest.raises(ValueError):
        _load(tmp_path, "active_mode: false\nburst_interval_s: 5\n")


@pytest.mark.parametrize("night", ["night_start_h: 6\nnight_end_h: 22\n",
                                   "night_start_h: 24\n", "night_end_h: -1\n"])
def test_a_night_that_does_not_span_midnight_is_refused(tmp_path, night):
    with pytest.raises(ValueError):
        _load(tmp_path, "active_mode: false\n" + night)
