/**
 * Route 53 + ACM + CloudFront edge stack for the HelloDJ platform.
 *
 * This stack provisions the edge/DNS foundation described in the design's
 * "Edge" subgraph and Route 53 naming model:
 *
 *   * A Route 53 public hosted zone for `hellodj.bot` (Requirement 12.1).
 *   * A per-environment DNS record derived from the single-source-of-truth
 *     `deriveEnvName(stage, region)` logic — `<stage>.<region>.hellodj.bot`
 *     (`beta`/`staging`/`production`, Requirements 12.2, 12.4).
 *   * For the production stage, an apex CNAME alias from the production
 *     environment name to the bare zone `hellodj.bot` (Requirement 12.3).
 *   * An ACM certificate (DNS-validated against the zone) covering the
 *     environment name so the edge can terminate TLS.
 *   * A CloudFront distribution acting as the managed edge cache for web
 *     static assets and HLS segments (Requirements 18.2, 18.4).
 *
 * The naming is region-parameterized so adding a region only introduces new,
 * non-colliding names with no redesign (Requirement 18.3).
 */
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as targets from 'aws-cdk-lib/aws-route53-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import {
  DeploymentStage,
  ZONE_NAME,
  apexAliasTarget,
  deriveEnvName,
} from './config';

/** Properties for {@link EdgeStack}. */
export interface EdgeStackProps extends cdk.StackProps {
  /** The deployment stage this edge belongs to (Beta/Staging/Production). */
  readonly stage: DeploymentStage;
  /** The AWS region the stage is provisioned in (e.g. `us-east-1`). */
  readonly region: string;
  /**
   * The shared Application Load Balancer fronting the EKS workloads (web-ui,
   * activity-backend). CloudFront uses this as the default origin for dynamic
   * requests, falling through to the ALB for Flask routes, API calls, and
   * WebSocket upgrades. When unset, all traffic routes to the S3 web-static
   * bucket (static-only mode, no dynamic app).
   */
  readonly applicationLoadBalancer?: elbv2.IApplicationLoadBalancer;
}

/**
 * Edge stack: Route 53 zone, per-env records, prod apex alias, ACM cert, and
 * a CloudFront distribution fronting web static + HLS origins.
 */
export class EdgeStack extends cdk.Stack {
  /** The `hellodj.bot` public hosted zone (Requirement 12.1). */
  public readonly hostedZone: route53.IHostedZone;
  /** The derived environment DNS name (`<stage>.<region>.hellodj.bot`). */
  public readonly envName: string;
  /** The ACM certificate covering the environment name. */
  public readonly certificate: acm.Certificate;
  /** The CloudFront distribution serving web static + HLS segments. */
  public readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props: EdgeStackProps) {
    super(scope, id, props);

    const { stage, region } = props;

    // Single-source-of-truth DNS name derivation (mirrors the Python
    // `dns_naming` logic): `<stage>.<region>.hellodj.bot`, or
    // `prod.<region>.hellodj.bot` for prod (Requirements 12.2, 12.4).
    this.envName = deriveEnvName(stage, region);

    // --- Route 53 hosted zone for hellodj.bot (Requirement 12.1) ----------
    // The `hellodj.bot` public hosted zone is a pre-existing, registrar-
    // delegated singleton (the domain's NS records at the registrar point at
    // this zone). We LOOK IT UP rather than create a new one: creating a second
    // `PublicHostedZone` for the same apex would produce a duplicate zone with
    // a different, undelegated NS set, and any ACM DNS-validation records
    // written into that undelegated zone would never resolve publicly — so the
    // certificate would hang forever in PENDING_VALIDATION. Looking up the
    // delegated zone means the ACM validation CNAME lands in the zone the
    // internet actually queries, so validation completes automatically.
    this.hostedZone = route53.HostedZone.fromLookup(this, 'HelloDjZone', {
      domainName: ZONE_NAME,
    });

    // --- S3 origins for web static assets and HLS segments ----------------
    // The activity-backend / hls-transcode components write HLS segments to
    // S3 and the web-ui ships static assets; CloudFront serves both from the
    // edge (Requirements 18.2, 18.4). Buckets are private and read via an
    // Origin Access Identity.
    const webStaticBucket = new s3.Bucket(this, 'WebStaticBucket', {
      bucketName: `hellodj-web-static-${stage}-${region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const hlsBucket = new s3.Bucket(this, 'HlsSegmentsBucket', {
      bucketName: `hellodj-hls-${stage}-${region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      // HLS segments are ephemeral; expire them to keep the origin lean.
      lifecycleRules: [{ expiration: cdk.Duration.days(1) }],
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // --- ACM certificate (DNS-validated against the zone) -----------------
    // Covers the environment name; for prod also covers the apex so the
    // CloudFront distribution can serve `hellodj.bot` directly.
    const isProd = stage === DeploymentStage.Production;
    this.certificate = new acm.Certificate(this, 'EdgeCertificate', {
      domainName: this.envName,
      subjectAlternativeNames: isProd ? [apexAliasTarget()] : undefined,
      validation: acm.CertificateValidation.fromDns(this.hostedZone),
    });

    // --- CloudFront distribution: managed edge cache ----------------------
    // Architecture:
    //   * Default behavior → ALB (dynamic Flask/HTMX app, Activity backend,
    //     API, WebSocket upgrades). This is the normal request path.
    //   * `/static/*` → S3 web-static bucket (cached CSS/JS/images/fonts).
    //   * `hls/*` → S3 HLS bucket (ephemeral transcoded segments).
    //
    // When no ALB is supplied (static-only mode), the default falls back to
    // the S3 web-static origin.
    const webStaticOrigin =
      origins.S3BucketOrigin.withOriginAccessControl(webStaticBucket);
    const hlsOrigin =
      origins.S3BucketOrigin.withOriginAccessControl(hlsBucket);

    // ALB origin for dynamic app traffic (Flask, activity WebSocket, API).
    // Uses HTTP (port 80) between CloudFront and the ALB; TLS terminates at
    // CloudFront (the ACM cert above). The ALB's listener handles HTTP → pods.
    const alb = props.applicationLoadBalancer;
    const defaultOrigin = alb
      ? new origins.HttpOrigin(alb.loadBalancerDnsName, {
          protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
          httpPort: 80,
        })
      : webStaticOrigin;

    // Alternate domain names the distribution answers on.
    const domainNames = isProd
      ? [this.envName, apexAliasTarget()]
      : [this.envName];

    this.distribution = new cloudfront.Distribution(this, 'EdgeDistribution', {
      comment: `HelloDJ edge cache (${this.envName})`,
      domainNames,
      certificate: this.certificate,
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      defaultBehavior: {
        origin: defaultOrigin,
        viewerProtocolPolicy:
          cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        // Dynamic content: forward all headers/cookies/query strings to the
        // ALB so Flask sessions, HTMX requests, and CSRF tokens work. No
        // caching at the edge for dynamic routes.
        cachePolicy: alb
          ? cloudfront.CachePolicy.CACHING_DISABLED
          : cloudfront.CachePolicy.CACHING_OPTIMIZED,
        originRequestPolicy: alb
          ? cloudfront.OriginRequestPolicy.ALL_VIEWER
          : undefined,
        allowedMethods: alb
          ? cloudfront.AllowedMethods.ALLOW_ALL
          : cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
      },
      additionalBehaviors: {
        // Static assets: long-lived, cache-optimized.
        'static/*': {
          origin: webStaticOrigin,
          viewerProtocolPolicy:
            cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
        },
        // HLS segments: cache-optimized, GET/HEAD only.
        'hls/*': {
          origin: hlsOrigin,
          viewerProtocolPolicy:
            cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        },
      },
    });

    // --- Route 53 records -------------------------------------------------
    // Per-env A alias record pointing the environment name at the
    // distribution (Requirements 12.2, 12.4).
    new route53.ARecord(this, 'EnvAliasRecord', {
      zone: this.hostedZone,
      recordName: this.envName,
      target: route53.RecordTarget.fromAlias(
        new targets.CloudFrontTarget(this.distribution),
      ),
      comment: `Environment record for ${this.envName}`,
    });

    // For the production stage, alias the apex (`hellodj.bot`) to the
    // production environment / distribution (Requirement 12.3). The design's
    // naming model describes this as a "CNAME (alias) from the production
    // environment name to hellodj.bot"; implemented as a Route 53 alias at
    // the apex targeting the same CloudFront distribution.
    if (isProd) {
      new route53.ARecord(this, 'ApexAliasRecord', {
        zone: this.hostedZone,
        recordName: apexAliasTarget(),
        target: route53.RecordTarget.fromAlias(
          new targets.CloudFrontTarget(this.distribution),
        ),
        comment: `Apex alias from ${apexAliasTarget()} to production edge`,
      });
    }

    // Expose key outputs for cross-stack / operator reference.
    new cdk.CfnOutput(this, 'HostedZoneId', {
      value: this.hostedZone.hostedZoneId,
    });
    new cdk.CfnOutput(this, 'EnvName', { value: this.envName });
    new cdk.CfnOutput(this, 'DistributionDomainName', {
      value: this.distribution.distributionDomainName,
    });
  }
}
