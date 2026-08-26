# kubectl layer (placeholder)

This directory is a placeholder asset for the EKS cluster's kubectl/Helm Lambda
layer required by `aws-cdk-lib`'s managed `eks.Cluster`.

Production deployments should supply a version-matched
`@aws-cdk/lambda-layer-kubectl-vXX` layer via `EksStackProps.kubectlLayer`
instead of relying on this placeholder. The layer must expose
`/opt/kubectl/kubectl` and `/opt/helm/helm`.
