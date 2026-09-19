import json
from doorbell.frames import Frame
from doorbell.assembler import Assembler

IMG = b"\xff\xd8octets-jpeg"
ATTR = json.dumps({"timestamp": 1789573468, "type": "on-demand"}).encode()

def _f(suffix, payload, t):
    return Frame(topic=f"ring/L/camera/D/{suffix}", payload=payload, received_at=t)

def test_image_then_attributes_gives_one_capture():
    a = Assembler()
    assert a.absorb(_f("snapshot/image", IMG, 100.0), 100.0) == []
    caps = a.absorb(_f("snapshot/attributes", ATTR, 100.3), 100.3)
    assert len(caps) == 1
    assert caps[0].kind == "on-demand"
    assert caps[0].timestamp == 1789573468
    assert caps[0].attributes_missing is False

def test_attributes_before_image_also_works():
    """The observed order is image then attributes, but we do not assume it."""
    a = Assembler()
    assert a.absorb(_f("snapshot/attributes", ATTR, 100.0), 100.0) == []
    caps = a.absorb(_f("snapshot/image", IMG, 100.2), 100.2)
    assert len(caps) == 1 and caps[0].kind == "on-demand"

def test_image_without_attributes_still_comes_out_after_the_window():
    """Never block the write: the best available image beats no image at all."""
    a = Assembler()
    a.absorb(_f("snapshot/image", IMG, 100.0), 100.0)
    assert a.expire(101.9) == []
    caps = a.expire(102.1)
    assert len(caps) == 1
    assert caps[0].kind == "unknown"
    assert caps[0].attributes_missing is True

def test_the_hash_covers_the_exact_bytes():
    import hashlib
    a = Assembler()
    a.absorb(_f("snapshot/image", IMG, 100.0), 100.0)
    caps = a.absorb(_f("snapshot/attributes", ATTR, 100.1), 100.1)
    assert caps[0].sha256 == hashlib.sha256(IMG).hexdigest()

def test_a_second_image_does_not_lose_the_first():
    """When a second image arrives, the first must come out as a Capture with
    attributes_missing=True, and its hash must be the one of its own bytes."""
    import hashlib
    a = Assembler()
    IMG2 = b"\xff\xd8octets-jpeg-2"

    # First image arrives
    caps = a.absorb(_f("snapshot/image", IMG, 100.0), 100.0)
    assert caps == []

    # Second image arrives before the attributes
    caps = a.absorb(_f("snapshot/image", IMG2, 100.1), 100.1)
    assert len(caps) == 1
    assert caps[0].data == IMG
    assert caps[0].sha256 == hashlib.sha256(IMG).hexdigest()
    assert caps[0].kind == "unknown"
    assert caps[0].attributes_missing is True

def test_second_attributes_count_the_first_as_orphaned():
    """When attributes arrive while others are already pending, the first
    ones must be counted as orphaned."""
    a = Assembler()
    ATTR2 = json.dumps({"timestamp": 1789573469, "type": "motion"}).encode()

    # First attributes arrive
    caps = a.absorb(_f("snapshot/attributes", ATTR, 100.0), 100.0)
    assert caps == []
    assert a.orphan_attrs == 0

    # Second attributes arrive before the image
    caps = a.absorb(_f("snapshot/attributes", ATTR2, 100.1), 100.1)
    assert caps == []
    assert a.orphan_attrs == 1
