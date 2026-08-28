/**
 * Cognito + OAuth + Secrets Manager stack for the HelloDJ AWS platform.
 *
 * Implements task 10.2 of the aws-saas-replatform plan. This stack provisions
 * the managed authentication model and the credential/secret storage that
 * replaces the legacy encrypted-SQLite credential store, plus the IAM task
 * roles that give the fleet keyless access to the managed AI services.
 *
 * What it creates:
 *  - A Cognito user pool that authenticates the administrator, initial
 *    registration, and account recovery, seeded (by the migration job, task
 *    19.1) with the Admin_Bootstrap_Credential. Day-to-day login of a
 *    registered/appointed user goes through Discord OAuth (handled in the
 *    web-ui component per `auth_routing.py`), NOT this pool — this pool is
 *    retained for admin/registration/recovery only (R8.2, R8.3, R8.5, R8.6,
 *    R19.3). An `admins` group isolates administrator identities.
 *  - A Cognito user pool client for the web-ui hosted-UI flows.
 *  - AWS Secrets Manager entries for the Discord bot token, the Tidal OAuth
 *    refresh token, the Spotify credentials, and the yt-cipher shared secret
 *    (design "Secrets"; R9.2 first-party Tidal refresh token storage). No
 *    tokens live in the datastore.
 *  - IAM roles usable as pod/task roles granting least-privilege access to
 *    Amazon Bedrock, Amazon Transcribe, and Amazon Polly, with NO static
 *    access keys — the fleet assumes these roles instead of storing AI/LLM
 *    API keys (design "Secrets": AI accessed via IAM task roles).
 *
 * The user pool, client, secrets, and AI task role are exposed as public
 * readonly properties so the migration job, web-ui, stream sidecars, and the
 * voice-pipeline workloads (later tasks) can consume them cross-stack.
 *
 * _Requirements: 8.2, 8.3, 8.5, 8.6 (Cognito for admin/registration/recovery),
 * 9.2 (HelloDJ-owned Tidal OAuth refresh secret), 19.1, 19.3 (admin bootstrap
 * credential lives in Cognito)._
 */
import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';
import { DeploymentStage } from './config';

/** Properties for {@link AuthStack}. */
export interface AuthStackProps extends cdk.StackProps {
  /** The deployment stage this auth model is provisioned for. */
  readonly stage: DeploymentStage;
}

/**
 * Cognito user pool + Discord/Tidal OAuth secrets + keyless AI IAM roles.
 */
export class AuthStack extends cdk.Stack {
  /** Cognito user pool: admin auth, initial registration, account recovery. */
  public readonly userPool: cognito.UserPool;

  /** Cognito user pool client used by the web-ui hosted-UI flows. */
  public readonly userPoolClient: cognito.UserPoolClient;

  /** Cognito group isolating administrator identities. */
  public readonly adminGroup: cognito.CfnUserPoolGroup;

  /** Secrets Manager entry: Discord bot token. */
  public readonly discordBotTokenSecret: secretsmanager.Secret;

  /** Secrets Manager entry: first-party Tidal OAuth refresh token (R9.2). */
  public readonly tidalRefreshSecret: secretsmanager.Secret;

  /** Secrets Manager entry: Spotify credentials. */
  public readonly spotifySecret: secretsmanager.Secret;

  /** Secrets Manager entry: yt-cipher shared secret. */
  public readonly ytCipherSecret: secretsmanager.Secret;

  /**
   * Secrets Manager entry: the Flask session signing key shared by all web-ui
   * replicas. A stable, shared key is required so an OAuth login started on
   * one pod and its callback landing on another pod validate the same signed
   * session cookie (otherwise the CSRF state is lost and the user is bounced
   * back to /login). Auto-generated once; surfaced to the web-ui via a
   * Kubernetes Secret in the workloads stack.
   */
  public readonly flaskSessionSecret: secretsmanager.Secret;

  /**
   * IAM role for the voice-pipeline (and any AI-consuming workload) granting
   * least-privilege, keyless access to Bedrock, Transcribe, and Polly.
   */
  public readonly aiTaskRole: iam.Role;

  constructor(scope: Construct, id: string, props: AuthStackProps) {
    super(scope, id, props);

    const { stage } = props;

    // ----- Cognito user pool ---------------------------------------------
    //
    // Retained for administrator authentication, initial registration, and
    // account recovery only (R8.2, R8.3, R8.5, R8.6). Self sign-up enables
    // registration; account recovery is via a verified email. Day-to-day
    // registered/appointed-user login uses Discord OAuth in the web-ui and
    // does not touch this pool.
    this.userPool = new cognito.UserPool(this, 'UserPool', {
      userPoolName: `hellodj-${stage}`,
      // Registration: people can self-register (R8.3).
      selfSignUpEnabled: true,
      // Sign in with email; email is the recovery channel (R8.5).
      signInAliases: { email: true, username: true },
      autoVerify: { email: true },
      standardAttributes: {
        email: { required: true, mutable: true },
      },
      // Account recovery via the verified email only (R8.5).
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      // Managed sign-up/recovery verification email.
      userVerification: {
        emailStyle: cognito.VerificationEmailStyle.CODE,
      },
      passwordPolicy: {
        minLength: 12,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      mfa: cognito.Mfa.OPTIONAL,
      mfaSecondFactor: { sms: false, otp: true },
      // Non-prod stages are disposable; prod retains identities on stack
      // teardown so the Admin_Bootstrap_Credential is never lost (R19).
      removalPolicy:
        stage === DeploymentStage.Production
          ? cdk.RemovalPolicy.RETAIN
          : cdk.RemovalPolicy.DESTROY,
    });

    // An `admins` group isolates administrator identities. The migration job
    // (task 19.1) seeds the Admin_Bootstrap_Credential user into this group so
    // the Platform_Owner's first AWS login authenticates through Cognito
    // (R19.1, R19.3).
    this.adminGroup = new cognito.CfnUserPoolGroup(this, 'AdminGroup', {
      userPoolId: this.userPool.userPoolId,
      groupName: 'admins',
      description:
        'HelloDJ administrators (Platform_Owner + appointed admins).',
      precedence: 0,
    });

    // A HelloDJ-owned Cognito domain hosts the managed sign-up / recovery UI.
    this.userPool.addDomain('HostedUiDomain', {
      cognitoDomain: {
        domainPrefix: `hellodj-${stage}-${this.account}`,
      },
    });

    // Web-ui client for the Cognito hosted-UI admin/registration/recovery
    // flows. Public client (no secret) suitable for the browser-side
    // authorization-code + PKCE flow the Flask web-ui drives.
    // The web-ui hosted-UI callback for each stage. The Flask app's Cognito
    // flow redirects to `<stage>.<region>.hellodj.bot/auth/cognito/callback`;
    // Cognito rejects any redirect_uri not in this list (redirect_mismatch),
    // so every stage's callback + logout URL must be registered here (R8.2).
    const region = this.region;
    const cognitoCallbackUrls = [
      `https://beta.${region}.hellodj.bot/auth/cognito/callback`,
      `https://staging.${region}.hellodj.bot/auth/cognito/callback`,
      `https://production.${region}.hellodj.bot/auth/cognito/callback`,
    ];
    const cognitoLogoutUrls = [
      'https://beta.' + region + '.hellodj.bot/',
      'https://staging.' + region + '.hellodj.bot/',
      'https://production.' + region + '.hellodj.bot/',
    ];

    this.userPoolClient = this.userPool.addClient('WebUiClient', {
      userPoolClientName: `hellodj-web-ui-${stage}`,
      generateSecret: false,
      authFlows: { userSrp: true },
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.PROFILE,
        ],
        callbackUrls: cognitoCallbackUrls,
        logoutUrls: cognitoLogoutUrls,
      },
      preventUserExistenceErrors: true,
    });

    // ----- Secrets Manager entries ---------------------------------------
    //
    // All source/service tokens live in Secrets Manager, not the datastore
    // (design "Secrets"). Each is created empty; the actual values are
    // populated out-of-band (console / migration / OAuth callback) so no
    // secret material is committed to source. Names are stage-scoped under
    // the `hellodj/` prefix.
    const secretName = (leaf: string): string => `hellodj/${stage}/${leaf}`;

    this.discordBotTokenSecret = new secretsmanager.Secret(
      this,
      'DiscordBotTokenSecret',
      {
        secretName: secretName('discord-bot-token'),
        description: 'HelloDJ Discord bot token.',
      },
    );

    // First-party Tidal OAuth refresh token (single app id). Refreshed via
    // the HelloDJ-owned OAuth integration (R9.2, R9.4).
    this.tidalRefreshSecret = new secretsmanager.Secret(
      this,
      'TidalRefreshSecret',
      {
        secretName: secretName('tidal-refresh'),
        description: 'HelloDJ first-party Tidal OAuth refresh token.',
      },
    );

    this.spotifySecret = new secretsmanager.Secret(this, 'SpotifySecret', {
      secretName: secretName('spotify'),
      description: 'HelloDJ Spotify credentials.',
    });

    this.ytCipherSecret = new secretsmanager.Secret(this, 'YtCipherSecret', {
      secretName: secretName('yt-cipher-secret'),
      description: 'HelloDJ yt-cipher shared secret (API_TOKEN).',
    });

    // The Flask session signing key shared by all web-ui replicas. Unlike the
    // source/service tokens above (created empty, populated out-of-band), this
    // one is auto-generated: it is an internal session key, not an external
    // credential, so a random generated value is exactly what's needed. The
    // workloads stack surfaces the value into a per-stage Kubernetes Secret the
    // web-ui reads via FLASK_SECRET_KEY (prevents OAuth-callback session loss
    // across replicas).
    this.flaskSessionSecret = new secretsmanager.Secret(
      this,
      'FlaskSessionSecret',
      {
        secretName: secretName('web-ui-flask-session'),
        description:
          'HelloDJ web-ui Flask session signing key (shared across replicas).',
        generateSecretString: {
          passwordLength: 64,
          excludePunctuation: true,
        },
      },
    );

    // ----- IAM task role for managed AI (keyless) ------------------------
    //
    // The voice-pipeline (and any AI-consuming workload) assumes this role to
    // call Bedrock, Transcribe, and Polly. There are NO static access keys:
    // the fleet gets short-lived credentials via role assumption (IRSA / pod
    // identity), which is why moving STT/intent/TTS to managed AI removes all
    // self-managed AI/LLM API keys from the platform (design "Secrets").
    //
    // The trust principal is left generic here (composed with the EKS OIDC
    // provider / pod-identity association when the workloads are wired in by
    // task 20.1). `ServicePrincipal('pods.eks.amazonaws.com')` matches the
    // EKS Pod Identity association model.
    this.aiTaskRole = new iam.Role(this, 'AiTaskRole', {
      roleName: `hellodj-ai-task-${stage}`,
      assumedBy: new iam.ServicePrincipal('pods.eks.amazonaws.com'),
      description:
        'Keyless task role for voice-pipeline AI access ' +
        '(Bedrock/Transcribe/Polly). No static credentials.',
    });

    // Least-privilege: only the specific invoke/synthesize/transcribe actions
    // the voice-pipeline needs, scoped as tightly as each service allows.
    this.aiTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockInvoke',
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
        ],
        // Model ARNs vary per region/account; scope to Bedrock foundation
        // models in this stack's region.
        resources: [
          `arn:aws:bedrock:${this.region}::foundation-model/*`,
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/*`,
        ],
      }),
    );
    this.aiTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'TranscribeStreaming',
        effect: iam.Effect.ALLOW,
        actions: [
          'transcribe:StartStreamTranscription',
          'transcribe:StartStreamTranscriptionWebSocket',
        ],
        // Transcribe streaming actions do not support resource-level scoping.
        resources: ['*'],
      }),
    );
    this.aiTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'PollySynthesize',
        effect: iam.Effect.ALLOW,
        actions: ['polly:SynthesizeSpeech'],
        // Polly SynthesizeSpeech does not support resource-level scoping.
        resources: ['*'],
      }),
    );

    // Let the AI task role read the secrets the voice/stream workloads need
    // at runtime (keyless: the role, not a stored key, gates access).
    this.discordBotTokenSecret.grantRead(this.aiTaskRole);
    this.tidalRefreshSecret.grantRead(this.aiTaskRole);
    this.spotifySecret.grantRead(this.aiTaskRole);
    this.ytCipherSecret.grantRead(this.aiTaskRole);

    // ----- Outputs --------------------------------------------------------
    new cdk.CfnOutput(this, 'UserPoolId', {
      value: this.userPool.userPoolId,
      description: 'HelloDJ Cognito user pool id (admin/registration/recovery).',
    });
    new cdk.CfnOutput(this, 'UserPoolClientId', {
      value: this.userPoolClient.userPoolClientId,
      description: 'HelloDJ Cognito web-ui client id.',
    });
    new cdk.CfnOutput(this, 'AiTaskRoleArn', {
      value: this.aiTaskRole.roleArn,
      description:
        'HelloDJ keyless AI task role ARN (Bedrock/Transcribe/Polly).',
    });
  }
}
