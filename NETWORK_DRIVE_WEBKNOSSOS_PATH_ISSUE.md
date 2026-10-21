## Context
When creating/uploading a local WebKnossos dataset from a Windows mapped network drive, WebKnossos appears to resolve the mapped path (`Z:\...`) into its UNC path (`\\server\share\...`). TensorStore then rejects the UNC path as an invalid local file kvstore path.

### Environment

- OS: Windows 11
- Data location: mapped network drive
- WebKnossos Python API, Release version (v3.6.0)
- TensorStore backend

### Example

Plugin/project path:

```text
Z:\ATLAS-examples\ZEINAB-ROI-w1-20nm-bsd\webknossos\ZEINAB-ROI-w1-20nm-bsd
```

Error path

```text
\\10.240.17.182\EM\ATLAS-examples\ZEINAB-ROI-w1-20nm-bsd\webknossos\ZEINAB-ROI-w1-20nm-bsd\color\1
```

Error:
```text
Error parsing object member "kvstore": Invalid file path: "\\10.240.17.182\EM\ATLAS-examples\ZEINAB-ROI-w1-20nm-bsd\webknossos\ZEINAB-ROI-w1-20nm-bsd\color\1"
```

## Suspected Cause
webKnossos path handling appears to resolve the mapped drive path before passing it to TensorStore, possibly via cheap_resolve() / Path.resolve(). On Windows, resolving a mapped drive such as Z:\... can produce the corresponding UNC path (\\server\share\...). That UNC path is then passed to TensorStore as a local file kvstore path, which TensorStore rejects.

## Expected Behavior
A local dataset stored on a mapped network drive should either:

- work using the mapped filesystem path, or
- fail early with a clear message indicating that UNC/network paths are unsupported.

## Current Behavior
<!--- What happens instead of the expected behavior? -->

## Steps to Reproduce the bug

1. On Windows, open or create a WebKnossos dataset on a mapped network drive, for example:
```text
Z:\ATLAS-examples\example-project\webknossos\example-project
```

2. Use the WebKnossos Python API to create/convert a local dataset at that mapped-drive path, for example through convert_zarr(...) with:
```text
convert_zarr(
    source_zarr_path=UPath(source_zarr_path),
    target_path=UPath(mapped_network_drive_dataset_path),
    ...
)
```
3. Trigger local dataset creation/downsampling/upload, for example:
```text
dataset = Dataset(mapped_network_drive_dataset_path, voxel_size=voxel_size, exist_ok=True)
dataset.downsample(...)
dataset.upload()
```
4. Observe that the mapped path is resolved internally to a UNC path and TensorStore fails with:
```text
Error parsing object member "kvstore": Invalid file path: "\\server\share\...\color\1"
```

## Current Workaround
We currently work around this by building, downsampling, and uploading the webKnossos dataset from a local cache path, then copying the completed dataset back to the network project folder. This avoids passing mapped-drive/UNC paths to TensorStore.

In our setup, the mapped network drive points to a Windows server with approximately 60 TB of storage and is our primary location for microscopy data. Requiring the dataset to be copied to a local disk before creating/downsampling/uploading it introduces substantial additional I/O and temporary storage requirements. For large datasets, the local copy may not fit on the workstation at all.

Being able to operate directly on the mapped network drive would therefore be very useful for our workflow.

## Question
Is UNC path support expected for local webKnossos datasets on Windows? If UNC paths are not supported by TensorStore, would it be possible for webKnossos to preserve mapped drive paths (e.g. Z:\...) rather than resolving them to UNC paths before constructing the TensorStore kvstore spec?
