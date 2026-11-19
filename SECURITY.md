# Security

## Reporting

Report suspected vulnerabilities in this fork privately, using GitHub's **Report a vulnerability** button on the repository's Security tab. Please don't open a public issue. For vulnerabilities in upstream OpenHop code that this fork didn't change, report to [openhop-dev](https://github.com/openhop-dev) as well.

## Design commitments

- **Secrets never leave through the API.**
  - Channel keys, private keys, MQTT passwords and bot secrets are write-only or file-only.
  - No endpoint or log returns them, including config export with secrets enabled.
- **Secret files are checked when opened:** the file must be regular, owned by the service user, have no group or world permissions, and not be a symlink.
- **Every fork endpoint requires authentication**, through the existing JWT or API key on `/api`.
- **Translation is not end-to-end.**
  - The bridge decrypts and re-encrypts between channels with different keys.
  - Channel authentication does not identify an individual sender.
  - Sender markers are a display convention, not proof that a message crossed a bridge.
- **Optional services fail closed:** bot receipts, reception evidence and Internet Link refuse rather than guess, and their failure never stops local RF.

## Never commit

Never commit any of these:
- channel keys or identity keys;
- certificates or CA keys;
- MQTT credentials;
- Wi-Fi passwords;
- enrollment state;
- live `config.yaml` files.

They are installation inputs. Use placeholders in docs and fixtures.
