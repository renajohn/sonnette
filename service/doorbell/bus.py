import time

import paho.mqtt.client as mqtt
from .frames import Frame

class Bus:
    def __init__(self, config, on_message):
        self.cfg, self.on_message = config, on_message
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id="doorbell-ai")
        self.c.will_set(f"{config.pub_prefix}/status", b"offline",
                        retain=True)
        self.c.on_connect = self._on_connect
        self.c.on_message = self._on_message

    def _on_connect(self, client, *_):
        for suffix in ("snapshot/image", "snapshot/attributes",
                        "motion/state", "ding/state", "status",
                        # ring-mqtt's heartbeat, for watch.py
                        "info/state"):
            client.subscribe(f"{self.cfg.ring_prefix}/{suffix}", qos=1)
        # The one command the service accepts: "keep this photo" (archive.py).
        client.subscribe(f"{self.cfg.pub_prefix}/archive/set", qos=1)
        # "show me this day" (summary.py's day view).
        client.subscribe(f"{self.cfg.pub_prefix}/day/set", qos=1)
        client.publish(f"{self.cfg.pub_prefix}/status", b"online", retain=True)

    def _on_message(self, client, userdata, msg):
        self.on_message(Frame(msg.topic, msg.payload, time.time()))

    def publish(self, topic, payload, retain=False):
        self.c.publish(topic, payload, qos=1, retain=retain)

    def start(self):
        self.c.connect(self.cfg.broker_host, self.cfg.broker_port, 30)
        self.c.loop_forever(retry_first_connection=True)

    def stop(self):
        self.c.publish(f"{self.cfg.pub_prefix}/status", b"offline", retain=True)
        self.c.disconnect()
