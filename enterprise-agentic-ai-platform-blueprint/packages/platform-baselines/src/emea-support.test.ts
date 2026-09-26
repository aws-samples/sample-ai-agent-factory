/**
 * EMEA component support-boundary tests.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { assertEmeaProfilePathSupported } from './emea-support';

describe('assertEmeaProfilePathSupported', () => {
  it('keeps the previously verified US path available', () => {
    expect(() =>
      assertEmeaProfilePathSupported('us-west-2', 'ExampleConstruct'),
    ).not.toThrow();
  });

  it.each(['eu-west-1', 'eu-west-2', 'eu-central-1'])(
    'fails closed in %s',
    (region) => {
      expect(() =>
        assertEmeaProfilePathSupported(region, 'ExampleConstruct'),
      ).toThrow(/not supported in EMEA/);
    },
  );
});
