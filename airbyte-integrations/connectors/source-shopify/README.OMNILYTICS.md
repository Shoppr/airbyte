# Omnilytics

## Differences

This forked source connector differs from the official source connector
in terms of:

- Implemented our own versioning
- Added `inventory_items` schema

## Deploying

To build and deploy the image, just run the deploy script:

```
./deploy
```

The deploy script tags the built image with the `io.airbyte.version`
version from `Dockerfile`. So, make sure the version is bumped before
deploying.
