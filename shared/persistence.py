import os
import logging

logger = logging.getLogger(__name__)

def append_peer_to_env(peer_ip: str):
    """
    Returns (was_updated, found_label)
    found_label is the .env key (like IP_C) if the IP was already mapped.
    """
    env_path = ".env"
    if not os.path.exists(env_path):
        logger.warning(f"No {env_path} found to update.")
        return False, None

    try:
        with open(env_path, "r") as f:
            lines = f.readlines()

        new_lines = []
        updated = False
        found_label = None

        conflict_url = f"http://{peer_ip}:8003"
        app_url = f"http://{peer_ip}:8080"

        keys_to_update = {
            "PEER_CONFLICT_URLS": conflict_url,
            "PEER_USER_URLS": app_url,
            "PEER_JOURNEY_URLS": app_url
        }

        found_keys = set()

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                new_lines.append(line)
                continue

            if "=" in line:
                key, val = line.split("=", 1)
                
                # Identity Detection: check if this IP is already mapped as IP_A, IP_B, etc.
                if key.startswith("IP_") and val.strip() == peer_ip:
                    found_label = key

                if key in keys_to_update:
                    found_keys.add(key)
                    target_url = keys_to_update[key]
                    
                    # Deduplicate
                    current_urls = [u.strip() for u in val.split(",") if u.strip()]
                    if target_url not in current_urls:
                        current_urls.append(target_url)
                        new_val = ",".join(current_urls)
                        new_lines.append(f"{key}={new_val}")
                        updated = True
                    else:
                        new_lines.append(line)
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        # Handle missing keys (if they weren't in .env yet)
        for key, target_url in keys_to_update.items():
            if key not in found_keys:
                new_lines.append(f"{key}={target_url}")
                updated = True

        if updated:
            with open(env_path, "w") as f:
                f.write("\n".join(new_lines) + "\n")
            logger.info(f"Successfully appended peer {peer_ip} to {env_path}")
        
        return updated, found_label

    except Exception as e:
        logger.error(f"Failed to update {env_path}: {e}")
        return False, None
