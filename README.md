# Steam Mod Remover

Steam Mod Remover compares a Steam game's installation directory against local Steam depot manifests or a saved file list, then identifies and removes extra files (likely a mods files) that were not installed through Steam. Due to how the program is designed, it may detect important files like save data as extra files. Please check the detected file list before proceeding.

Steam 게임 설치 폴더를 로컬 Steam Depot manifest 또는 사용자가 저장한 기준 목록과 비교해, 목록에 없는 (스팀을 통해 설치되지 않은) 파일을 찾아 정리합니다. 작동 원리 상, 세이브 파일 등의 중요한 파일이 삭제될 수 있습니다. 파일 목록을 확인 후에 삭제를 진행해 주세요.


### Basic usage

1. Click `Browse`, and select your Steam library's `steamapps` directory.

2. Click `Load installed games`.

3. Select the game from the game dropdown.

   The app automatically reads that game's locally installed depot list. Use `Find local installed depots` to refresh it manually if needed.

4. Select the required depots. All installed depots are selected by default.

5. Click `Load file lists from selected Depots`.

   The app reads Steam's local `depotcache` manifests, builds a list of original files and folders, and compares it with the installed game directory.

6. Review the results and check the items you want to remove.

7. Select either `Delete` or `Backup` as the removal destination.

   - `Delete`: moves items to the Windows Recycle Bin.
   - `Backup`: moves items to the folder chosen with `Choose backup folder`, while preserving their original relative paths.

8. Click `Remove checked items`.

### File categories and selection

Click the checkbox in the first column to select or clear an individual file.

| Category | Meaning | Initially selected |
| --- | --- | --- |
| Extra file | Extras file that are not in the depots saved file list | Yes |
| Protected | Extra files but matches a protection rule | No |
| Matches baseline (keep) | A file that exists in the baseline | Cannot be selected |

`Select all extra files` checks every extra files, and `Select all protected` checks every protected file. These buttons only select files; files are removed only when `Remove checked items` button is pressed.

### Protection rules

When protection rules are enabled, non-baseline files whose filenames contain a protected term are categorized as `Protected`. The defaults include terms related to saves, settings, logs, screenshots, and the `.ini`, `.cfg`, and `.json` extensions.

Protected terms can be added, edited, or removed as a comma-separated list. For example: `save, profile, .ini, my_important_file`.

Protection rules are only a safety measure and cannot identify every type of file perfectly. Always review the file list before removing anything.

### Saving and loading a file list json

#### Save the current file list as a json file

To use the current state as a baseline without relying on depot data:

1. Select a game.
2. Click `Load current file list (as is)`.
3. Click `Save current file lists as json` to save it as a JSON file.

This saves every currently installed file and folder as the baseline. If mods are already installed when it is saved, those mod files will also become part of the baseline.

Load a saved baseline using `Load file lists from json file`.

---

## 한국어

## 기본 사용 방법

1. `찾아보기`를 눌러 steamapps 폴더를 선택합니다.

2. `설치 게임 불러오기`를 누릅니다.

3. 게임 드롭다운에서 정리할 게임을 선택합니다.

   선택하면 해당 게임의 로컬 설치 Depot 목록을 자동으로 읽습니다. 필요하면 `로컬 설치 Depot 조회` 버튼으로 다시 읽을 수 있습니다.

4. 필요한 Depot를 선택합니다. 기본값은 설치된 Depot 전체 선택입니다.

5. `선택 Depot 기준 목록 불러오기`를 누릅니다.

   Steam 클라이언트의 로컬 `depotcache` manifest를 읽어 순정 파일·폴더 목록을 만들고, 실제 게임 설치 폴더와 비교합니다.

6. 결과 목록을 확인하고 제거할 항목을 체크합니다.

7. 제거 위치를 `삭제` 또는 `백업`으로 선택합니다.

   - `삭제`: Windows 휴지통으로 이동합니다.
   - `백업`: `백업 폴더 선택`으로 지정한 폴더에 이동합니다. 원래 상대 경로는 보존됩니다.

8. `체크된 항목 제거`를 누르고, 경고 창에서 `예`를 선택합니다.

## 파일 목록 분류와 선택

목록 첫 번째 열의 체크박스를 클릭하면 개별 파일을 선택하거나 해제할 수 있습니다.

| 분류 | 의미 | 초기 선택 상태 |
| --- | --- | --- |
| 삭제 후보 | 기준 목록에 없는 일반 파일 | 선택 |
| 보호됨 | 보호 규칙과 일치한 기준 외 파일 | 선택 안 함 |
| 기준 일치 (유지) | 기준 목록에 존재하는 파일 | 선택 불가 |

`삭제 후보 모두 선택`은 모든 삭제 후보를, `보호됨 모두 선택`은 모든 보호 파일을 체크합니다. 두 버튼은 선택만 하며 실제 파일 이동은 `체크된 항목 제거`에서만 일어납니다.

## 보호 규칙

보호 규칙이 켜져 있으면, 파일명에 보호 텍스트가 포함된 기준 외 파일은 `보호됨`으로 분류됩니다. 기본값에는 세이브, 설정, 로그, 스크린샷과 관련된 텍스트 및 `.ini`, `.cfg`, `.json` 확장자가 포함됩니다.

보호 텍스트는 쉼표로 구분해 직접 추가·수정·삭제할 수 있습니다. 예를 들어 `save, profile, .ini, my_important_file`처럼 입력합니다.

보호 규칙은 안전장치일 뿐 파일의 성격을 완벽히 판별하지는 못합니다. 제거 전 파일 목록을 반드시 검토하세요.


## 기준 목록 저장과 불러오기
### 현재 파일 목록을 기준으로 저장

SteamDB/Depot 정보와 무관하게 현재 상태를 기준으로 삼으려면 다음을 사용합니다.

1. 게임을 선택합니다.
2. `현재 파일 목록 불러오기`를 누릅니다.
3. `현재 기준 목록 저장`을 눌러 JSON 파일로 저장합니다.

이 방식은 현재 설치된 모든 파일과 폴더를 기준으로 저장합니다. 이미 모드가 설치된 상태에서 저장하면 그 모드 파일도 기준 목록에 포함됩니다.

저장한 기준 목록은 `기준 목록 파일에서 불러오기`로 다시 불러옵니다.


