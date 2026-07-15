import React from 'react';
import ReactDOM from 'react-dom';
import { Editor } from '@atlaskit/editor-core';
import { ReactRenderer } from '@atlaskit/renderer';

type ADFDocument = { version: 1; type: 'doc'; content: unknown[] };
type EditorOptions = {
  value: ADFDocument;
  mode?: 'edit' | 'preview';
  onChange?: (value: ADFDocument) => void;
};

function ADFEditor({ value, mode = 'edit', onChange }: EditorOptions) {
  if (mode === 'preview') {
    return <ReactRenderer document={value} appearance="full-page" />;
  }
  return (
    <Editor
      appearance="full-page"
      defaultValue={value}
      allowTextColor
      allowTables={{ advanced: true }}
      allowExpand={{ allowInsertion: true }}
      allowPanel
      allowRule
      allowCodeBlocks={{ enableKeybindingsForIDE: true }}
      onChange={(view) => onChange?.(view.state.doc.toJSON() as ADFDocument)}
    />
  );
}

function mount(target: Element, options: EditorOptions) {
  ReactDOM.render(<ADFEditor {...options} />, target);
  return () => {
    ReactDOM.unmountComponentAtNode(target);
  };
}

const api = { mount };
(globalThis as typeof globalThis & { CodeReviewerADF?: typeof api }).CodeReviewerADF = api;
export { mount };
