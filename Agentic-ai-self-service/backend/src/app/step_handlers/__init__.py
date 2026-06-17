"""Step Functions step handler modules for deployment orchestration.

Each module in this package is a Lambda handler function invoked by the
Step Functions state machine during a deployment workflow. Handlers follow
a consistent pattern:

1. Read input from the Step Functions event dict
2. Perform the operation by calling the appropriate service module
3. Update the Deployment_State_Table with current_step via DeploymentStateStore
4. Return output dict for the next step in the state machine
5. Handle errors gracefully and return error info

Requirements: 3.2, 3.3, 3.4, 3.5, 3.6
"""
