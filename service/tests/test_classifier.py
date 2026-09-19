import hashlib
from doorbell.frames import Capture
from doorbell.classifier import Classifier, Verdict

C = Capture(b"JPEG", hashlib.sha256(b"JPEG").hexdigest(), 1000, "motion", False)

class _Fake:
    def __init__(self, reply=None, error=None):
        self.reply, self.error = reply, error
    def __call__(self, url, body, timeout):
        if self.error:
            raise self.error
        return self.reply

def test_a_conforming_reply_gives_ok():
    c = Classifier("http://x", send=_Fake("O,N,O"))
    v = c.classify(C)
    assert (v.status, v.person, v.animal, v.package) == ("ok", True, False, True)

def test_a_timeout_still_gives_a_verdict():
    """A consumer must be able to distinguish 'not yet' from 'never'."""
    c = Classifier("http://x", send=_Fake(error=TimeoutError()))
    v = c.classify(C)
    assert v.status == "timeout" and v.person is None

def test_a_refused_connection_gives_unavailable():
    c = Classifier("http://x", send=_Fake(error=ConnectionError()))
    assert c.classify(C).status == "unavailable"

def test_a_non_conforming_reply_keeps_the_raw_text():
    """This is the raw material for the next prompt revision."""
    c = Classifier("http://x", send=_Fake("je vois un monsieur"))
    v = c.classify(C)
    assert v.status == "unparseable" and "monsieur" in v.raw

def test_a_malformed_200_reply_still_produces_a_verdict():
    """A model that is down is more likely to return degraded JSON than a
    clean network exception. Without a verdict, the classification loop
    would die."""
    def _broken_shape(*a):
        raise KeyError("choices")
    c = Classifier("http://x", send=_broken_shape)
    v = c.classify(C)
    assert v.status == "unparseable"
    assert v.person is None
    assert "KeyError" in v.raw
