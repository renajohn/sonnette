import base64, json, re, time, urllib.error, urllib.request
from dataclasses import dataclass
from .frames import Capture

# Copied WORD FOR WORD from /config/scripts/sonnette-classer.py in production.
PROMPT = ("Analyse cette image de camera de porte d entree. Reponds UNIQUEMENT "
          "par trois valeurs separees par des virgules, dans cet ordre : "
          "personne, animal, paquet. Chaque valeur vaut O si l element est "
          "visible sur l image, N sinon. Compte O des qu un element est visible meme partiellement ou en partie hors cadre : un bras, une jambe, une silhouette au bord de l image suffisent pour repondre O. Exemple de reponse : O,N,O . "
          "colis = O uniquement pour un carton d expedition ferme, une boite "
          "en carton brun, une enveloppe matelassee ou un sachet de transporteur. "
          "colis = N pour un sac de courses, un cabas, un sac en papier, un sac "
          "en tissu, un panier, un sac a main, un sac a dos, une valise, une "
          "poubelle, un bac de tri, un telephone, des cles, un outil, un vetement, "
          "de la nourriture. "
          "Le mobilier permanent du porche - paillasson, tapis, tuyau d "
          "arrosage, etagere, outils de jardin, plantes, velo range - ne "
          "compte pas comme un colis.")

PATTERN = re.compile(r"\b([ON])\s*,\s*([ON])\s*,\s*([ON])\b")

@dataclass(frozen=True)
class Verdict:
    status: str
    person: bool | None = None
    animal: bool | None = None
    package: bool | None = None
    raw: str | None = None
    latency_ms: int = 0

def _send_http(url: str, body: bytes, timeout: float) -> str:
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["choices"][0]["message"]["content"].upper()

class Classifier:
    def __init__(self, url: str, timeout_s: float = 8.0,
                 prompt_version: str = "v5", send=_send_http):
        self.url, self.timeout_s = url, timeout_s
        self.prompt_version, self._send = prompt_version, send

    def classify(self, c: Capture) -> Verdict:
        body = json.dumps({
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64," +
                           base64.b64encode(c.data).decode()}}]}],
            "max_tokens": 12, "temperature": 0}).encode()
        t0 = time.perf_counter()
        try:
            reply = self._send(self.url, body, self.timeout_s)
        except TimeoutError:
            return Verdict("timeout")
        except (ConnectionError, urllib.error.URLError, OSError):
            return Verdict("unavailable")
        except (ValueError, KeyError, IndexError, AttributeError, TypeError) as e:
            # A malformed 200 response must never let classify() exit without
            # a verdict: the caller runs in a single worker thread, and the
            # exception would kill the classification loop.
            return Verdict("unparseable", raw=f"{type(e).__name__}: {e}"[:200])
        ms = int((time.perf_counter() - t0) * 1000)
        m = PATTERN.search(reply)
        if not m:
            return Verdict("unparseable", raw=reply[:200], latency_ms=ms)
        p, a, k = (v == "O" for v in m.groups())
        return Verdict("ok", p, a, k, latency_ms=ms)
