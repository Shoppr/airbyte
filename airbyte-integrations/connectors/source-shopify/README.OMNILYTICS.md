# Omnilytics

## Differences

This forked source connector differs from the official source connector
in terms of:

- Implemented our own versioning
- Added `inventory_items` schema

## Updating

Add upstream remote:

```
git remote add upstream git@github.com:airbytehq/airbyte.git
```

To merge the updates from upstream:

```
git checkout master
git merge upstream/master
```

Fix the conflicts, and push:

```
git push
```

## Testing

Follow the instructions for locally running the connector in the
[README](./README.md). You should have the `secrets`

Setup `secrets/config.json`:

```
{
  "shop": "shop",
  "start_date": "2020-07-22",
  "api_password": "password"
}
```

Generate `secrets/discovery.json`:

```
python3 main.py discover --config secrets/config.json > secrets/discovery.json
```

Generate `secrets/catalog.json` with the stream you want to test. For
instance, for `inventory_items` in `incremental` sync mode:

```
cat secrets/discovery.json | jq '
  {
    "streams": .catalog.streams |
      map(select(.name == "inventory_items")) |
      map({
        "stream": .,
        "sync_mode": "incremental",
        "cursor_field": .default_cursor_field,
        "destination_sync_mode": "append"
      })
  }
' > secrets/catalog.json
```

Create `secrets/state.json` with empty state for now:

```
{}
```

Run the connector, and it should sync all the records since the start
date in `secrets/config.json`:

```
python3 main.py read --config secrets/config.json --catalog secrets/catalog.json --state secrets/state.json
```

To test the incremental syncing, update the `secrets/state.json`:

```
{
  "inventory_items": {
    "updated_at": "2021-07-21T23:34:00+08:00"
  }
}
```

Run the connector again, and this time it should only sync the latest
records:

```
python3 main.py read --config secrets/config.json --catalog secrets/catalog.json --state secrets/state.json
```

## Deploying

To build and deploy the image, just run the deploy script:

```
./deploy
```

The deploy script tags the built image with the `io.airbyte.version`
version from `Dockerfile`. So, make sure the version is bumped before
deploying.
