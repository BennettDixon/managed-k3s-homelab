# Unscoped Secrets
These secrets are not scoped by a namespace.

They should only be consumed by a Kustomize that properly namespaces them.

This way they can be reused in multiple namespaces, e.g for pulling Harbor images.

You may want to consider having multiple projects & registries in Harbor, in which case you may want to separate the login for each registry, meaning you would need different secrets in AWS & not be able to use this overlay.