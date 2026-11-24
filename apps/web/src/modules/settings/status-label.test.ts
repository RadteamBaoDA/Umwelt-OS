import { describe, expect, it } from 'vitest';
import enUs from '../../../locales/en-us/uiSweep.json';
import viVi from '../../../locales/vi-vi/uiSweep.json';
import { CORE_SERVICES } from '@/modules/observability/system-status-model';
import { serviceLabelKeys } from './status-label';

describe('service labels', () => {
  it('labels every core health component in both locales', () => {
    for (const service of CORE_SERVICES) {
      const key = serviceLabelKeys[service as keyof typeof serviceLabelKeys];
      expect(key, service).toBeDefined();
      expect(enUs as Record<string, string>, service).toHaveProperty(key);
      expect(viVi as Record<string, string>, service).toHaveProperty(key);
    }
  });
});
