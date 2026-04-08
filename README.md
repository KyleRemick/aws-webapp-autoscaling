## aws-webapp-autoscaling

Simple Python scripts that deploy a basic web page on EC2 instances behind an Application Load Balancer with Auto Scaling.

### Architecture
- Default VPC and two subnets
- Application Load Balancer (HTTP)
- Target group and listener
- Launch template and Auto Scaling group
- EC2 user data installs Apache and serves a small HTML page

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
