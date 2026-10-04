import type { ComponentProps } from 'react';
import { RouterProvider } from 'react-router-dom';
import { MetaProvider } from './components/PageMeta';

export type AppRouter = ComponentProps<typeof RouterProvider>['router'];

/** Client and test root: a data router wrapped in the page-meta provider. */
export function App({ router }: { router: AppRouter }) {
  return (
    <MetaProvider>
      <RouterProvider router={router} />
    </MetaProvider>
  );
}
