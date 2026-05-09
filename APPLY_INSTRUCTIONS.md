# How to apply this LionMesh credibility update

Copy these files into the root of your local `lionmesh` repository.

Recommended Windows workflow:

```powershell
cd "C:\Users\David Doerfel\Downloads\lionmesh"

git status

# Copy/overwrite the files from this patch package into the repository root.
# Then run:
python scripts\rebrand_lionmesh_to_lionmesh.py

git status
git add README.md STATUS.md .github\workflows\ci.yml scripts\rebrand_lionmesh_to_lionmesh.py REVIEW_COMMIT_MESSAGE.txt APPLY_INSTRUCTIONS.md
git add daemon control phy config setup webapp requirements.txt

git commit -m "Improve project credibility and clarify experimental status"
git push
```

If your local folder is not yet a Git repository, clone the GitHub repo first:

```powershell
cd "C:\Users\David Doerfel\Downloads"
git clone https://github.com/daviddoerfel/lionmesh.git
cd lionmesh
```

Then copy the patch files into that cloned folder and run the commands above.
