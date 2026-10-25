PROJECT := SIM Anywhere.xcodeproj
SCHEME := SIMAnywhere
BUNDLE_ID := net.janlay.simanywhere
TEAM_ID ?= 28466ZMF85
DEBUG_DESTINATION ?= generic/platform=iOS Simulator
DEBUG_SIGNING ?= NO
RELEASE_DESTINATION ?= generic/platform=iOS
RELEASE_SIGNING ?= YES
BUILD_ROOT ?= /Volumes/SanDisk/tmp/sim-anywhere
ARCHIVE_DIR ?= $(HOME)/Library/Developer/Xcode/Archives/SIMAnywhere
BUILD_NUMBER := $(shell sed -nE 's/^[[:space:]]*CURRENT_PROJECT_VERSION:[[:space:]]*"?([0-9]+)"?.*/\1/p' project.yml | head -1)
ARCHIVE_PATH := $(ARCHIVE_DIR)/SIMAnywhere-$(BUILD_NUMBER).xcarchive
EXPORT_PATH := $(ARCHIVE_DIR)/export-$(BUILD_NUMBER)
GIT_SHORT_HASH := $(shell git rev-parse --short HEAD 2>/dev/null || printf unknown)
XCODEBUILD := xcodebuild -allowProvisioningUpdates -allowProvisioningDeviceRegistration
ASC_KEY_PATH ?= $(HOME)/.appstoreconnect/private_keys/AuthKey_$(ASC_KEY_ID).p8
ASC_AUTH = $(if $(and $(ASC_KEY_ID),$(ASC_ISSUER_ID)),-authenticationKeyPath "$(ASC_KEY_PATH)" -authenticationKeyID "$(ASC_KEY_ID)" -authenticationKeyIssuerID "$(ASC_ISSUER_ID)")
TESTFLIGHT_GROUP ?= Foundation
TESTFLIGHT_NOTES ?= Scripts/TestFlightWhatToTest.txt

.DEFAULT_GOAL := debug
.PHONY: debug release archive upload testflight bump generate check-clean-tree check-signing-session check-asc

generate:
	xcodegen generate

debug: generate
	$(XCODEBUILD) -project '$(PROJECT)' -scheme $(SCHEME) -configuration Debug \
		-destination '$(DEBUG_DESTINATION)' -derivedDataPath '$(BUILD_ROOT)/debug' \
		DEVELOPMENT_TEAM=$(TEAM_ID) CODE_SIGNING_ALLOWED=$(DEBUG_SIGNING) \
		SIM_ANYWHERE_GIT_SHORT_HASH=$(GIT_SHORT_HASH) build

# project.yml is the source of truth; XcodeGen recreates the project and Info.plist.
# A failed Release build still consumes its build number, so the next attempt is unique.
bump:
	python3 Scripts/bump-build.py project.yml '$(ARCHIVE_DIR)'

release:
	$(MAKE) bump
	$(MAKE) generate
	$(XCODEBUILD) -project '$(PROJECT)' -scheme $(SCHEME) -configuration Release \
		-destination '$(RELEASE_DESTINATION)' -derivedDataPath '$(BUILD_ROOT)/release' \
		DEVELOPMENT_TEAM=$(TEAM_ID) CODE_SIGNING_ALLOWED=$(RELEASE_SIGNING) \
		SIM_ANYWHERE_GIT_SHORT_HASH=$(GIT_SHORT_HASH) build

# Archive the current, committed Release build number; run make release first.
archive: check-signing-session check-clean-tree generate
	@test -n '$(BUILD_NUMBER)' || { echo 'project.yml has no build number'; exit 1; }
	@test ! -e '$(ARCHIVE_PATH)' || { echo 'Archive already exists: $(ARCHIVE_PATH)'; exit 1; }
	umask 022 && $(XCODEBUILD) -project '$(PROJECT)' -scheme $(SCHEME) -configuration Release \
		-destination 'generic/platform=iOS' -derivedDataPath '$(BUILD_ROOT)/archive' \
		-archivePath '$(ARCHIVE_PATH)' DEVELOPMENT_TEAM=$(TEAM_ID) \
		SIM_ANYWHERE_GIT_SHORT_HASH=$(GIT_SHORT_HASH) archive
	umask 022 && $(XCODEBUILD) -exportArchive -archivePath '$(ARCHIVE_PATH)' \
		-exportPath '$(EXPORT_PATH)' -exportOptionsPlist Scripts/ExportOptions.plist $(ASC_AUTH)
	@echo 'IPA: $(EXPORT_PATH)'

upload: check-asc
	@ipa=$$(find '$(EXPORT_PATH)' -maxdepth 1 -type f -name '*.ipa' -print -quit 2>/dev/null); \
		test -n "$$ipa" || { echo 'IPA not found. Run make archive first.'; exit 1; }; \
		xcrun altool --upload-app --type ios --file "$$ipa" --apiKey '$(ASC_KEY_ID)' --apiIssuer '$(ASC_ISSUER_ID)'

# Wait for App Store Connect processing, then assign the uploaded build to a group.
testflight: check-asc
	@test -f '$(ARCHIVE_PATH)/Info.plist' || { echo 'Archive not found. Run make archive first.'; exit 1; }
	@test -f '$(TESTFLIGHT_NOTES)' || { echo 'TestFlight notes not found: $(TESTFLIGHT_NOTES)'; exit 1; }
	swift Scripts/testflight.swift '$(BUNDLE_ID)' \
		"$$('/usr/libexec/PlistBuddy' -c 'Print :ApplicationProperties:CFBundleShortVersionString' '$(ARCHIVE_PATH)/Info.plist')" \
		"$$('/usr/libexec/PlistBuddy' -c 'Print :ApplicationProperties:CFBundleVersion' '$(ARCHIVE_PATH)/Info.plist')" \
		'$(TESTFLIGHT_GROUP)' '$(TESTFLIGHT_NOTES)'

check-signing-session:
	@test "$$(launchctl managername)" = Aqua || { echo 'Archiving requires a macOS graphical session to access the signing keychain.'; exit 1; }

check-clean-tree:
	@test -z "$$(git status --porcelain)" || { echo 'Commit the version and code before archiving so the Git revision matches the archive.'; exit 1; }

check-asc:
	@test -n '$(ASC_KEY_ID)' -a -n '$(ASC_ISSUER_ID)' || { echo 'Set ASC_KEY_ID and ASC_ISSUER_ID.'; exit 1; }
	@test -f '$(ASC_KEY_PATH)' || { echo 'Local App Store Connect API key not found.'; exit 1; }
