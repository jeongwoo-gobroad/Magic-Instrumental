# Landing page — developer deployment branch

This branch contains the independent introduction/install page for Magic-Instrumental. General users should open the page itself or the application's main README.

- `index.html`: standalone HTML/CSS; no account, upload, runtime JavaScript or application backend dependency.
- `LICENSE`: GPL-3.0-only for original page code.
- The visible Session link opens the existing GitHub Pages player.
- The footer includes the requested public author/CV attribution and the main repository's third-party notice link.

The integrated source is `landing/index.html` on `main`. Publish a reviewed copy here and deploy this branch to a separate static web root. Operator configuration belongs in developer documentation and private server files, never in the page. See the main branch's [deployment guide](https://github.com/jeongwoo-gobroad/Magic-Instrumental/blob/main/docs/DEPLOYMENT_BRANCHES.md).

Update the versioned direct release ZIP link only after that immutable release asset exists. Verify desktop/phone layout, download response, footer links and supported-format wording. Supported PDF structure is single-part measures with standard staff or TAB, including paired notation/TAB. Do not claim automatic pitch recognition from TAB numerals or direct PNG/JPG/JPEG score input; image-based PDFs can also fail recognition.
