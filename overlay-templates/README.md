# Unscoped Secrets
These secrets are not scoped by a namespace.

They should only be consumed by a Kustomize that properly namespaces them.

This way they can be reused in multiple namespaces, e.g for pulling Harbor images.
