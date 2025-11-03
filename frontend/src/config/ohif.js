// OHIF Viewer Configuration
// This config is used when integrating OHIF Viewer for DICOM viewing

export const ohifConfig = {
  extensions: [],
  modes: [],
  showStudyList: true,
  maxNumberOfWebWorkers: 3,
  // DICOMweb server configuration
  servers: {
    dicomWeb: [
      {
        name: 'DICOMweb Server',
        wadoUriRoot: process.env.VITE_DICOM_WADO_URI_ROOT || 'http://localhost:8000/api/v1/dicomweb',
        qidoRoot: process.env.VITE_DICOM_QIDO_ROOT || 'http://localhost:8000/api/v1/dicomweb',
        wadoRoot: process.env.VITE_DICOM_WADO_ROOT || 'http://localhost:8000/api/v1/dicomweb',
        qidoSupportsIncludeField: true,
        imageRendering: 'wadors',
        thumbnailRendering: 'wadors',
        requestTransferSyntax: 'compressed',
      },
    ],
  },
  defaultDataSourceName: 'dicomweb',
}

export default ohifConfig

