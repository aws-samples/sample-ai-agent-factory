/**
 * ModalHost - registry-driven modal renderer.
 * Renders active modal from zustand store using the modal registry.
 */

import { Suspense } from 'react';
import { MODAL_REGISTRY, type ModalKey } from './modalRegistry';

interface ModalHostProps {
  modalKey: ModalKey | null;
  modalProps: unknown;
}

export function ModalHost({ modalKey, modalProps }: ModalHostProps) {
  if (!modalKey) return null;

  const ModalComponent = MODAL_REGISTRY[modalKey];
  if (!ModalComponent) {
    console.error(`Modal not found in registry: ${modalKey}`);
    return null;
  }

  // Type assertion needed here because modalProps type is heterogeneous across modals
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const Component = ModalComponent as React.ComponentType<any>;

  return (
    <Suspense fallback={null}>
      <Component {...(modalProps as object)} />
    </Suspense>
  );
}
