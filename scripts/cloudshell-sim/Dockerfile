# Approximation of AWS CloudShell for testing install.sh: Amazon Linux 2023, AWS CLI v2, python3,
# zip, git, curl, no Docker and no project dependencies. (Real CloudShell also ships Node.js and the
# CDK CLI; install.sh only needs them for a first-time CDK bootstrap.)
FROM public.ecr.aws/amazonlinux/amazonlinux:2023
RUN dnf install -y awscli-2 python3 zip unzip git tar gzip findutils which procps-ng shadow-utils \
 && dnf clean all \
 && useradd -m cloudshell-user
USER cloudshell-user
WORKDIR /home/cloudshell-user
ENV AWS_EXECUTION_ENV=CloudShell
