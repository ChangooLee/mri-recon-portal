import React from 'react'

function FileUpload({ onUpload, uploading = false }) {
  return (
    <div>
      <label style={{
        display: 'inline-block',
        padding: '12px 24px',
        background: uploading ? '#ccc' : '#4285f4',
        color: 'white',
        borderRadius: '5px',
        cursor: uploading ? 'not-allowed' : 'pointer',
        fontSize: '16px'
      }}>
        {uploading ? 'Uploading...' : 'Select DICOM Files'}
        <input
          type="file"
          multiple
          accept=".dcm,.dicom"
          onChange={onUpload}
          disabled={uploading}
          style={{ display: 'none' }}
        />
      </label>
    </div>
  )
}

export default FileUpload

