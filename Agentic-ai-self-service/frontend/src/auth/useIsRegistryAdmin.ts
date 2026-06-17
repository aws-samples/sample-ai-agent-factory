/**
 * Hook to determine if the current user is a registry admin.
 * Reads cognito:groups from the ID token — admin if it contains
 * 'registry-admin' or legacy 'org-admin'.
 */

import { useState, useEffect } from 'react';
import { fetchAuthSession } from 'aws-amplify/auth';

export function useIsRegistryAdmin(): boolean {
  const [isAdmin, setIsAdmin] = useState(false);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const session = await fetchAuthSession();
        const idToken = session.tokens?.idToken;
        if (!idToken || cancelled) return;

        const groups = idToken.payload['cognito:groups'];
        let groupList: string[] = [];

        // Defensive: groups can be an array, a JSON string, or a comma/space-delimited string
        if (Array.isArray(groups)) {
          groupList = groups as string[];
        } else if (typeof groups === 'string') {
          try {
            groupList = JSON.parse(groups);
          } catch {
            // Not JSON — split by comma or space
            groupList = groups.split(/[,\s]+/).map((g) => g.trim()).filter(Boolean);
          }
        }

        const admin = groupList.some((g) => g === 'registry-admin' || g === 'org-admin');
        if (!cancelled) {
          setIsAdmin(admin);
        }
      } catch {
        // Auth failure or local dev — default to false (developer persona)
        if (!cancelled) {
          setIsAdmin(false);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  return isAdmin;
}
