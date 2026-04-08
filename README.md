## aws-webapp-autoscaling

### Summary
This project demonstrates practical AWS automation by provisioning EC2 capacity behind an Application Load Balancer and Auto Scaling Group with Python and boto3. It covers launch templates, target groups, multi-AZ scaling, and repeatable deployment and cleanup workflows.

### Architecture
- Default VPC and two subnets
- Application Load Balancer (HTTP)
- Target group and listener
- Launch template and Auto Scaling group
- EC2 user data installs Apache and serves a small HTML page

Architecture flow: User -> Application Load Balancer -> Auto Scaling Group -> EC2 instances

### Concepts Used/Learned
- Deploying EC2 instances using Launch Templates
- Configuring an Application Load Balancer with target groups
- Creating Auto Scaling Groups across multiple Availability Zones
- Automating infrastructure lifecycle using boto3 deployment scripts

### Deployment
1. Make sure your AWS credentials are configured (e.g. `aws configure`).
2. Create a Python venv and install dependencies:
   - `python -m venv .venv`
   - `.\.venv\Scripts\activate`
   - `pip install -r requirements.txt`
3. Deploy:
   - `python deploy.py --region us-east-1`
   - Optional: `python deploy.py --region us-east-1 --name-prefix my-webapp`
   - Optional: `python deploy.py --region us-east-1 --vpc-id vpc-123 --subnet-ids subnet-aaa,subnet-bbb`
   - Optional: `python deploy.py --region us-east-1 --create-vpc`
4. Open the printed load balancer DNS name in a browser.

### Cleanup
- `python cleanup.py --region us-east-1`

### What this demonstrates
Basic AWS automation with boto3, including networking setup, load balancing, and auto scaling.
