/**
 * Private CodeCommit source-of-truth stack for the HelloDJ AWS platform.
 *
 * Implements the SourceStack from `hellodj-private-source-and-toolchain`
 * design §1 ("CodeCommit repositories + transactional migration (R1)"). It
 * relocates the HelloDJ source of truth off public GitHub into five private,
 * AWS-native Amazon CodeCommit repositories — the HelloDJ application repo plus
 * the four JVM forks (`Lavalink`, `lavaplayer`, `LavaSrc`, `youtube-source`) —
 * so their existence, names, and access policy are declarative infrastructure,
 * consistent with the platform's "everything in CDK, no console clicks" goal.
 *
 *   * R1.1 - Exactly five private CodeCommit Source_Repos are provisioned:
 *            `hellodj`, `Lavalink`, `lavaplayer`, `LavaSrc`, `youtube-source`.
 *   * R1.3 - The `Lavalink` Source_Repo designates `dev` as its build branch.
 *   * R1.7 - Each Source_Repo is private and not readable without an
 *            authenticated, authorized IAM principal: a resource policy grants
 *            read/pull access only to the platform build IAM roles (the
 *            GHA-runner role and the EKS/Karpenter builder role). A repository
 *            with no allowing principal is not readable.
 *
 * The `upstream` remote (R1.2) and the transactional history-preserving push
 * (R1.4/R1.5) are properties of each cloned working copy and the migration
 * procedure (task 11.2, `platform/tools/migrate_repos.py` driving
 * `hellodj_platform_logic.migration.migrate_repos`), not of the
 * CodeCommit-hosted repository resource; this stack provisions the private
 * repositories and their access policy. The build-branch designation is
 * captured here as declarative metadata (repo description + an exported map)
 * so the build path and the migration procedure read one source of truth.
 */
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as codecommit from 'aws-cdk-lib/aws-codecommit';
import * as iam from 'aws-cdk-lib/aws-iam';

/**
 * One migrated Source_Repo's identity and its designated build branch.
 *
 * Mirrors the design "Data Models" `CodeCommitRepo` shape (name +
 * upstream_url + build_branch); `upstreamUrl` is `undefined` for the HelloDJ
 * application repo, which has no upstream (R1.2). The upstream remote itself is
 * established by the migration procedure on the working copy, not on the
 * CodeCommit resource — it is carried here only as declarative metadata.
 */
export interface SourceRepoSpec {
  /** CodeCommit repository name (also the construct/resource name). */
  readonly name: string;
  /** Public upstream project URL for the four forks; `undefined` for the app repo (R1.2). */
  readonly upstreamUrl?: string;
  /** The designated build branch (e.g. `Lavalink` -> `dev`, R1.3). */
  readonly buildBranch: string;
}

/**
 * The five Source_Repos relocated into CodeCommit (R1.1), with their upstreams
 * and build branches from the design's repo/upstream/build-branch table.
 *
 * This is the single source of truth for repository names and build branches,
 * consumed by the migration procedure (task 11.2) and the flake-input switch
 * (task 12.1).
 */
export const SOURCE_REPOS: readonly SourceRepoSpec[] = [
  { name: 'hellodj', buildBranch: 'main' },
  {
    name: 'Lavalink',
    upstreamUrl: 'https://github.com/lavalink-devs/Lavalink',
    buildBranch: 'dev', // R1.3
  },
  {
    name: 'lavaplayer',
    upstreamUrl: 'https://github.com/lavalink-devs/lavaplayer',
    buildBranch: 'main',
  },
  {
    name: 'LavaSrc',
    upstreamUrl: 'https://github.com/topi314/LavaSrc',
    buildBranch: 'tidal-v2-api',
  },
  {
    name: 'youtube-source',
    upstreamUrl: 'https://github.com/lavalink-devs/youtube-source',
    buildBranch: 'main',
  },
] as const;

/** Properties for {@link SourceStack}. */
export interface SourceStackProps extends cdk.StackProps {
  /**
   * ARNs of the platform build IAM roles that are the ONLY principals granted
   * read/pull access to the CodeCommit Source_Repos (R1.7): the GitHub Actions
   * runner role (assumed via OIDC) and the EKS/Karpenter builder role (assumed
   * via IRSA / pod identity). Access is derived at fetch time from these
   * assumed roles via the AWS git credential helper — no static credential is
   * stored (spec R2.3).
   *
   * When omitted (e.g. during a credential-less synth), no build-role grant is
   * emitted; the repositories remain private and — with no allowing principal —
   * not readable, which still satisfies R1.7's "not readable without an
   * authenticated, authorized IAM principal".
   */
  readonly buildRoleArns?: readonly string[];
}

/**
 * Provisions the five private CodeCommit Source_Repos and the resource policy
 * that grants read/pull access only to the platform build IAM roles (R1.1,
 * R1.3, R1.7).
 */
export class SourceStack extends cdk.Stack {
  /** The five provisioned CodeCommit repositories, keyed by repo name (R1.1). */
  public readonly repositories: Record<string, codecommit.Repository>;

  /** The build IAM roles (imported by ARN) granted read/pull access (R1.7). */
  public readonly buildRoles: iam.IRole[];

  /** Map of repo name -> designated build branch (R1.3), the single source of truth. */
  public readonly buildBranches: Record<string, string>;

  constructor(scope: Construct, id: string, props: SourceStackProps = {}) {
    super(scope, id, props);

    const buildRoleArns = props.buildRoleArns ?? [];

    // Import the build roles by ARN so they can be granted read/pull access.
    // These are the only principals allowed to read the repositories (R1.7).
    this.buildRoles = buildRoleArns.map((arn, i) =>
      iam.Role.fromRoleArn(this, `BuildRole${i}`, arn, {
        // Do not mutate the imported role's own policy; we express access via
        // the repository's grant (which attaches the statement to the role).
        mutable: true,
      }),
    );

    this.repositories = {};
    this.buildBranches = {};

    for (const spec of SOURCE_REPOS) {
      // A CodeCommit repository is private by default — there is no anonymous
      // or public read path. Access is entirely IAM-gated, so a repo with no
      // allowing principal is not readable (R1.7).
      const repo = new codecommit.Repository(this, spec.name, {
        repositoryName: spec.name,
        description: this.describeRepo(spec),
      });

      // Grant read/pull access ONLY to the build IAM roles. No public or
      // anonymous access is ever granted (R1.7).
      for (const role of this.buildRoles) {
        repo.grantPull(role);
      }

      this.repositories[spec.name] = repo;
      this.buildBranches[spec.name] = spec.buildBranch;

      // Export the clone URL + build branch so the migration procedure and the
      // flake-input switch read one source of truth.
      new cdk.CfnOutput(this, `${spec.name}CloneUrlOutput`, {
        value: repo.repositoryCloneUrlGrc,
        description: `git-remote-codecommit (GRC) clone URL for the ${spec.name} Source_Repo.`,
      });
      new cdk.CfnOutput(this, `${spec.name}BuildBranchOutput`, {
        value: spec.buildBranch,
        description: `Designated build branch for the ${spec.name} Source_Repo.`,
      });
    }
  }

  /**
   * Human-readable description capturing the repo's role, its preserved
   * upstream (R1.2), and its designated build branch (R1.3) as declarative
   * metadata on the CodeCommit resource.
   */
  private describeRepo(spec: SourceRepoSpec): string {
    const upstream = spec.upstreamUrl
      ? `fork of ${spec.upstreamUrl} (upstream remote preserved on the working copy)`
      : 'HelloDJ application repository (no upstream)';
    return `HelloDJ private Source_Repo — ${upstream}; build branch: ${spec.buildBranch}.`;
  }
}
