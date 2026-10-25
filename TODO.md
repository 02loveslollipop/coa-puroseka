# Pending AWS redeployment

- [ ] Wait for the user to refresh Learner Lab AWS CLI credentials.
- [ ] Check the existing CloudFormation stacks and take a database backup.
- [ ] Deploy with `scripts/aws.py deploy --app-only --configure-access`.
- [ ] Verify independent query/login/chat roles, denied writes, admin/demo login, existing sessions and token usage.
