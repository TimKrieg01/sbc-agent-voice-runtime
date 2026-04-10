# Asterisk VM Cheat Sheet

Use these commands on the Linux VM that runs Asterisk.

## 1. Check whether Asterisk is running

```bash
sudo systemctl status asterisk
```

## 2. Start Asterisk

```bash
sudo systemctl start asterisk
```

## 3. Stop Asterisk

```bash
sudo systemctl stop asterisk
```

## 4. Restart Asterisk

```bash
sudo systemctl restart asterisk
```

## 5. Reload the systemd unit after service-file changes

```bash
sudo systemctl daemon-reload
```

## 6. Enable Asterisk on boot

```bash
sudo systemctl enable asterisk
```

## 7. Disable Asterisk on boot

```bash
sudo systemctl disable asterisk
```

## 8. Open the Asterisk CLI

```bash
sudo asterisk -rvvv
```

## 9. Confirm Asterisk version and uptime

```bash
sudo asterisk -rx "core show uptime"
sudo asterisk -rx "core show version"
```

## 10. Reload all Asterisk config without full restart

```bash
sudo asterisk -rx "core reload"
```

## 11. Reload only PJSIP after trunk or endpoint changes

```bash
sudo asterisk -rx "pjsip reload"
```

## 12. Check loaded PJSIP endpoints, auths, and identifies

```bash
sudo asterisk -rx "pjsip show endpoints"
sudo asterisk -rx "pjsip show auths"
sudo asterisk -rx "pjsip show identifies"
```

## 13. Watch Asterisk logs live

```bash
sudo tail -f /var/log/asterisk/messages
```

## 14. Watch service logs through systemd

```bash
sudo journalctl -u asterisk -f
```

## 15. Turn on verbose SIP debugging during a test call

```bash
sudo asterisk -rvvv
```

Then inside the Asterisk CLI:

```text
pjsip set logger on
core set verbose 5
core set debug 5
```

Turn it back down after testing:

```text
pjsip set logger off
core set verbose 2
core set debug 0
```

## Useful file paths

```text
/etc/asterisk/pjsip.conf
/etc/asterisk/extensions.conf
/etc/asterisk/sorcery.conf
/etc/asterisk/extconfig.conf
/var/log/asterisk/messages
```

## Good quick workflow for trunk testing

```bash
sudo systemctl status asterisk
sudo asterisk -rx "pjsip reload"
sudo asterisk -rx "pjsip show endpoints"
sudo asterisk -rvvv
```

In a second terminal:

```bash
sudo tail -f /var/log/asterisk/messages
```
