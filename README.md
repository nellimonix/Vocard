# YaMusic (Vocard fork)

YaMusic is a Discord music bot based on [Vocard](https://github.com/ChocoMeow/Vocard).
This fork uses Yandex Music as its default search provider and includes Russian and English localization.

## Setup
Follow the [Vocard setup guide](https://docs.vocard.xyz/latest/bot/setup).
Copy `settings Example.json` to `settings.json` and supply your own credentials.

### Second Lavalink node

The optional `docker-compose.lavalink-secondary.yml` runs a second instance on the existing `vocard` Docker network. It uses the local `vocard-lavalink` image by default. Set `LAVALINK_SECONDARY_IMAGE` to select a different local image.

Create `data/lavalink-secondary/application.yml` with the second account's token and a password for the second node. Give the Lavalink container user read access to the file.

```yaml
plugins:
    lavasrc:
        yandexmusic:
            accessToken: "SECOND_ACCOUNT_TOKEN"
lavalink:
    server:
        password: "SECOND_NODE_PASSWORD"
```

Start the node with `docker compose -f docker-compose.lavalink-secondary.yml up -d --wait`. In the bot's `/debug` panel, open **Nodes**, then **Add**. Use `SECONDARY` as the identifier, `lavalink-secondary` as the host, `2333` as the port, `false` for **Secure**, and the second node's Lavalink password. Save the same values as an entry under `nodes` in `settings.json` for future bot starts.

## License
[MIT](LICENSE). Original Vocard copyright notices are preserved.
