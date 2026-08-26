import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { NetworkStack } from '../lib/network-stack';
import { DeploymentStage } from '../lib/config';

/**
 * CDK assertion tests for the network stack (task 9.4).
 *
 * These synthesize the {@link NetworkStack} into a CloudFormation template and
 * assert the shape the design mandates: a multi-AZ VPC, an internet-facing
 * Application Load Balancer for HTTP/S app traffic, and a Network Load Balancer
 * for the Discord gateway TCP sockets (Requirement 2.1 — the orchestrator
 * fleet runs in this VPC behind these load balancers).
 */

const TEST_ENV = { account: '111111111111', region: 'us-east-1' };

function synthNetwork(props?: { maxAzs?: number }): Template {
  const app = new cdk.App();
  const stack = new NetworkStack(app, 'TestNetworkStack', {
    stage: DeploymentStage.Beta,
    env: TEST_ENV,
    maxAzs: props?.maxAzs,
  });
  return Template.fromStack(stack);
}

describe('NetworkStack (task 9.4, R2.1)', () => {
  test('provisions a VPC', () => {
    const template = synthNetwork();
    template.resourceCountIs('AWS::EC2::VPC', 1);
  });

  test('spans multiple AZs (a subnet per AZ in each tier)', () => {
    // Default maxAzs is 3: a public + private-egress subnet per AZ => 6 subnets.
    const template = synthNetwork();
    template.resourceCountIs('AWS::EC2::Subnet', 6);
    // A SINGLE shared NAT gateway serves egress for all private subnets — the
    // Shared_Foundation cost floor (one NAT, not one-per-AZ). Subnets and the
    // ALB/NLB stay multi-AZ (maxAzs=3) but egress collapses to one NAT.
    // (hellodj-shared-foundation R4.3.)
    template.resourceCountIs('AWS::EC2::NatGateway', 1);
  });

  test('provisions an internet-facing Application Load Balancer', () => {
    const template = synthNetwork();
    template.hasResourceProperties(
      'AWS::ElasticLoadBalancingV2::LoadBalancer',
      {
        Type: 'application',
        Scheme: 'internet-facing',
      },
    );
  });

  test('provisions a Network Load Balancer for gateway sockets', () => {
    const template = synthNetwork();
    template.hasResourceProperties(
      'AWS::ElasticLoadBalancingV2::LoadBalancer',
      {
        Type: 'network',
        Scheme: 'internet-facing',
      },
    );
  });

  test('provisions exactly one shared ALB and one shared NLB (singletons)', () => {
    const template = synthNetwork();

    // Total load balancers is exactly two: the shared ALB + the shared NLB.
    // (Shared_Foundation R1.5/R1.6 — one shared ALB, one shared NLB.)
    template.resourceCountIs(
      'AWS::ElasticLoadBalancingV2::LoadBalancer',
      2,
    );

    // Disambiguate by Type: exactly one application (ALB) and one network (NLB).
    const lbs = template.findResources(
      'AWS::ElasticLoadBalancingV2::LoadBalancer',
    );
    const types = Object.values(lbs).map((r) => r.Properties?.Type);
    expect(types.filter((t) => t === 'application')).toHaveLength(1);
    expect(types.filter((t) => t === 'network')).toHaveLength(1);
  });

  test('ALB security group allows public HTTPS ingress', () => {
    const template = synthNetwork();
    template.hasResourceProperties('AWS::EC2::SecurityGroup', {
      SecurityGroupIngress: Match.arrayWith([
        Match.objectLike({
          CidrIp: '0.0.0.0/0',
          FromPort: 443,
          ToPort: 443,
          IpProtocol: 'tcp',
        }),
      ]),
    });
  });

  test('respects a lower maxAzs for cheaper non-prod stages', () => {
    const template = synthNetwork({ maxAzs: 2 });
    // 2 AZs * (public + private-egress) = 4 subnets. NAT stays pinned at the
    // single shared gateway regardless of AZ count (R4.3 cost floor).
    template.resourceCountIs('AWS::EC2::Subnet', 4);
    template.resourceCountIs('AWS::EC2::NatGateway', 1);
  });
});
