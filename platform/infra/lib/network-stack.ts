/**
 * VPC + multi-AZ networking construct for the HelloDJ AWS platform.
 *
 * Implements task 9.2 of the aws-saas-replatform plan: a multi-AZ VPC with
 * public and private-with-egress subnets, an Application Load Balancer (for
 * HTTP/S app traffic: web-ui, activity-backend, orchestrator) and a Network
 * Load Balancer (for the Discord gateway TCP sockets), plus the security
 * groups that gate traffic between them and the EKS fleet.
 *
 * The VPC and load balancers are exposed as public readonly properties so the
 * EKS and edge stacks (sibling tasks 9.3, 9.5) can consume them as shared
 * networking primitives.
 *
 * _Requirements: 1.1 (all infra in CDK), 2.1 (single orchestrator fleet runs
 * in this VPC), 18.1 (single region at launch, region-parameterized so
 * additional regions add a new VPC without redesign)._
 */
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Construct } from 'constructs';
import { DeploymentStage } from './config';

/** Properties for {@link NetworkStack}. */
export interface NetworkStackProps extends cdk.StackProps {
  /**
   * Optional, retained for backward compatibility only.
   *
   * The network stack is a stage-independent Shared_Foundation singleton: it
   * provisions the VPC, ALB, and NLB exactly once for all three software
   * stages (`hellodj-beta`/`-staging`/`-production`). Nothing in this stack
   * consumes the stage — it fed only stage name tags previously — so callers
   * that still pass it continue to compile while the singleton composition
   * simply omits it.
   */
  readonly stage?: DeploymentStage;
  /**
   * Number of Availability Zones to span. Defaults to 3 for a multi-AZ
   * production posture; can be lowered for cheaper non-prod stages.
   */
  readonly maxAzs?: number;
  /** CIDR block for the VPC. Defaults to `10.0.0.0/16`. */
  readonly cidr?: string;
}

/**
 * Multi-AZ VPC with an ALB (app HTTP/S) and an NLB (Discord gateway sockets).
 *
 * Subnet layout per AZ:
 *  - a public subnet (ingress for the load balancers, NAT gateways), and
 *  - a private subnet with egress (the EKS node groups and workloads).
 */
export class NetworkStack extends cdk.Stack {
  /** The multi-AZ VPC shared across the platform fleet. */
  public readonly vpc: ec2.Vpc;

  /** Application Load Balancer for HTTP/S app traffic. */
  public readonly applicationLoadBalancer: elbv2.ApplicationLoadBalancer;

  /** Network Load Balancer for the Discord gateway TCP sockets. */
  public readonly networkLoadBalancer: elbv2.NetworkLoadBalancer;

  /** Security group attached to the ALB (public HTTP/S ingress). */
  public readonly albSecurityGroup: ec2.SecurityGroup;

  /** Security group attached to the EKS node/workload fleet. */
  public readonly fleetSecurityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: NetworkStackProps) {
    super(scope, id, props);

    const maxAzs = props.maxAzs ?? 3;
    const cidr = props.cidr ?? '10.0.0.0/16';

    // Multi-AZ VPC with a public tier (load balancers, NAT) and a
    // private-with-egress tier (EKS node groups + workloads). Subnets and the
    // shared ALB/NLB stay multi-AZ (maxAzs=3), but a SINGLE shared NAT gateway
    // serves egress for all private subnets — the Shared_Foundation cost floor
    // (one NAT, not one-per-AZ), accepting the reduced egress HA posture for
    // the recorded saving. (hellodj-shared-foundation R4.3; R1.5/R1.6 keep the
    // one shared ALB and one shared NLB below unchanged.)
    this.vpc = new ec2.Vpc(this, 'Vpc', {
      ipAddresses: ec2.IpAddresses.cidr(cidr),
      maxAzs,
      natGateways: 1,
      subnetConfiguration: [
        {
          name: 'public',
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          name: 'private-egress',
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 20,
        },
      ],
    });

    // ----- Security groups ------------------------------------------------

    // ALB security group: accepts public HTTP/S ingress from anywhere.
    this.albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
      vpc: this.vpc,
      description: 'HelloDJ ALB ingress (public HTTP/S).',
      allowAllOutbound: true,
    });
    this.albSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      'HTTPS from the internet',
    );
    this.albSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(80),
      'HTTP from the internet (redirect to HTTPS at the listener)',
    );

    // Fleet security group: the EKS node groups + workloads. Accepts app
    // traffic only from the ALB; intra-fleet traffic is allowed for the
    // chatty Lavalink <-> transcode <-> orchestrator paths kept on-node/AZ.
    this.fleetSecurityGroup = new ec2.SecurityGroup(
      this,
      'FleetSecurityGroup',
      {
        vpc: this.vpc,
        description: 'HelloDJ EKS node/workload fleet.',
        allowAllOutbound: true,
      },
    );
    this.fleetSecurityGroup.addIngressRule(
      this.albSecurityGroup,
      ec2.Port.tcpRange(1024, 65535),
      'App traffic from the ALB to fleet target ports',
    );
    this.fleetSecurityGroup.connections.allowInternally(
      ec2.Port.allTraffic(),
      'Intra-fleet traffic (Lavalink/transcode/orchestrator)',
    );

    // ----- Application Load Balancer (HTTP/S app traffic) -----------------
    //
    // Internet-facing ALB in the public subnets, fronting web-ui,
    // activity-backend, and the orchestrator API. CloudFront (edge stack)
    // sits in front of this for cached content (Requirement 18.2).
    this.applicationLoadBalancer = new elbv2.ApplicationLoadBalancer(
      this,
      'AppLoadBalancer',
      {
        vpc: this.vpc,
        internetFacing: true,
        securityGroup: this.albSecurityGroup,
        vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      },
    );

    // Default HTTP listener on port 80 — CloudFront connects here. Returns a
    // 503 until the EKS workloads register targets via the AWS LB Controller
    // or CDK target groups. This prevents CloudFront 504 (gateway timeout)
    // when the ALB has no listener at all.
    this.applicationLoadBalancer.addListener('HttpListener', {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
      defaultAction: elbv2.ListenerAction.fixedResponse(503, {
        contentType: 'text/plain',
        messageBody: 'HelloDJ beta — workloads deploying',
      }),
    });

    // ----- Network Load Balancer (Discord gateway sockets) ----------------
    //
    // The Discord gateway is a long-lived WSS/TCP socket path. An NLB gives
    // us stable, low-overhead L4 handling for those sockets, distinct from
    // the L7 ALB used for request/response app traffic.
    this.networkLoadBalancer = new elbv2.NetworkLoadBalancer(
      this,
      'GatewayLoadBalancer',
      {
        vpc: this.vpc,
        internetFacing: true,
        vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      },
    );

    // ----- Outputs --------------------------------------------------------
    // Export the shared networking primitives so the EKS/edge stacks (sibling
    // tasks) can consume them cross-stack when composed in the app.
    new cdk.CfnOutput(this, 'VpcId', {
      value: this.vpc.vpcId,
      description: 'HelloDJ platform VPC id.',
    });
    new cdk.CfnOutput(this, 'AlbDnsName', {
      value: this.applicationLoadBalancer.loadBalancerDnsName,
      description: 'HelloDJ Application Load Balancer DNS name.',
    });
    new cdk.CfnOutput(this, 'NlbDnsName', {
      value: this.networkLoadBalancer.loadBalancerDnsName,
      description: 'HelloDJ Network Load Balancer DNS name (gateway sockets).',
    });
  }
}
