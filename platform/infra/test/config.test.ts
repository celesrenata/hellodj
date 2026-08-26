import { resolveConfig, DeploymentStage, ZONE_NAME, DEFAULT_REGION } from '../lib/config';

describe('platform config', () => {
  test('defaults to Beta in the launch region', () => {
    const config = resolveConfig({ region: DEFAULT_REGION, stage: DeploymentStage.Beta });
    expect(config.stage).toBe(DeploymentStage.Beta);
    expect(config.region).toBe(DEFAULT_REGION);
  });

  test('exposes the hellodj.bot zone name', () => {
    expect(ZONE_NAME).toBe('hellodj.bot');
  });

  test('honors explicit overrides', () => {
    const config = resolveConfig({ stage: DeploymentStage.Production, region: 'eu-west-1' });
    expect(config.stage).toBe(DeploymentStage.Production);
    expect(config.region).toBe('eu-west-1');
  });
});
