import { CodeBlock } from './CodeBlock';
import { DocImage } from './DocImage';
import { ResponsiveTable } from './ResponsiveTable';
import { SmartLink } from './SmartLink';
import { TableHeaderCell } from './TableHeaderCell';

/** Component map passed to every compiled Markdown module. */
export const mdxComponents = {
  pre: CodeBlock,
  a: SmartLink,
  img: DocImage,
  table: ResponsiveTable,
  th: TableHeaderCell,
};
