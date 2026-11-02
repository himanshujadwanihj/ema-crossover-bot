import json
import os

CONFIG_FILE = "/data/dynamic_config.json" if os.path.isdir("/data") else "dynamic_config.json"

def get_dyn_config():
    default_cfg = {
        "ADX_THRESHOLD": 20.0, 
        "VOLUME_MULTIPLIER": 1.2,
        "BODY_RATIO": 0.25,
        "RSI_MIN": 45.0,
        "RSI_MAX": 75.0
    }
    if not os.path.exists(CONFIG_FILE):
        return default_cfg
    try:
        with open(CONFIG_FILE, 'r') as f:
            cfg = json.load(f)
            # Merge defaults for any missing keys
            for k, v in default_cfg.items():
                if k not in cfg:
                    cfg[k] = v
            return cfg
    except:
        return default_cfg

def set_dyn_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)
