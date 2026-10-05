"""PULSE seed sub-package.

Houses the CloudFormation custom-resource seed handler that populates the
``pulse-kitchen`` snapshot table on stack create/update, mirroring LUMI's
``Custom::SeedData`` pattern. The handler lives in the shared PULSE Lambda
deployment package (``pulse-backend.zip``) so it is packaged and referenced the
same way every other PULSE Lambda is.
"""
