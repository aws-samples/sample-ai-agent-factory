# Workshop Studio Assets

This folder is used for staging static assets that are synced to a dedicated S3
bucket for the workshop (nested CloudFormation templates and the `source/` code
that is pulled into the Code Editor IDE). `deploy-cfn.sh` populates and syncs it
automatically — see the repository `README.md` for how the deploy flow works.
