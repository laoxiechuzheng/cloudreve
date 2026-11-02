package model

import (
	"errors"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/cloudreve/Cloudreve/v3/pkg/cache"
	"github.com/cloudreve/Cloudreve/v3/pkg/conf"
	"github.com/gin-gonic/gin"
	"github.com/jinzhu/gorm"
	"github.com/stretchr/testify/assert"
)

func TestShare_Create(t *testing.T) {
	asserts := assert.New(t)
	share := Share{UserID: 1}

	// 成功
	{
		mock.ExpectBegin()
		mock.ExpectExec("INSERT(.+)").WillReturnResult(sqlmock.NewResult(2, 1))
		mock.ExpectCommit()
		id, err := share.Create()
		asserts.NoError(mock.ExpectationsWereMet())
		asserts.NoError(err)
		asserts.EqualValues(2, id)
	}

	// 失败
	{
		mock.ExpectBegin()
		mock.ExpectExec("INSERT(.+)").WillReturnError(errors.New("error"))
		mock.ExpectRollback()
		id, err := share.Create()
		asserts.NoError(mock.ExpectationsWereMet())
		asserts.Error(err)
		asserts.EqualValues(0, id)
	}
}

func TestGetShareByHashID(t *testing.T) {
	asserts := assert.New(t)
	conf.SystemConfig.HashIDSalt = ""

	// 成功
	{
		mock.ExpectQuery("SELECT(.+)").
			WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(1))
		res := GetShareByHashID("x9T4")
		asserts.NoError(mock.ExpectationsWereMet())
		asserts.NotNil(res)
	}

	// 查询失败，自定义链接也没有
	{
		mock.ExpectQuery("SELECT(.+)").
			WillReturnError(errors.New("error"))
		mock.ExpectQuery("SELECT(.+)custom_key(.+)").
			WithArgs("x9T4").
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		res := GetShareByHashID("x9T4")
		asserts.NoError(mock.ExpectationsWereMet())
		asserts.Nil(res)
	}

	// HashID 对应的分享已删除，按自定义链接找到另一个分享
	{
		mock.ExpectQuery("SELECT(.+)").
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		mock.ExpectQuery("SELECT(.+)custom_key(.+)").
			WithArgs("x9T4").
			WillReturnRows(sqlmock.NewRows([]string{"id", "custom_key"}).AddRow(5, "x9T4"))
		res := GetShareByHashID("x9T4")
		asserts.NoError(mock.ExpectationsWereMet())
		asserts.EqualValues(5, res.ID)
	}

	// 不是 HashID，按自定义链接查找
	{
		mock.ExpectQuery("SELECT(.+)custom_key(.+)").
			WithArgs("98k-win").
			WillReturnRows(sqlmock.NewRows([]string{"id", "custom_key"}).AddRow(3, "98k-win"))
		res := GetShareByHashID("98k-win")
		asserts.NoError(mock.ExpectationsWereMet())
		asserts.EqualValues(3, res.ID)
	}

	// 格式不对，不查询数据库
	{
		res := GetShareByHashID("a/b")
		asserts.NoError(mock.ExpectationsWereMet())
		asserts.Nil(res)
	}

}

func TestShare_Key(t *testing.T) {
	asserts := assert.New(t)
	conf.SystemConfig.HashIDSalt = ""

	asserts.Equal("jRfM", (&Share{Model: gorm.Model{ID: 1}}).Key())
	asserts.Equal("98k-win", (&Share{Model: gorm.Model{ID: 1}, CustomKey: "98k-win"}).Key())
}

func TestCheckShareKeyAvailable(t *testing.T) {
	asserts := assert.New(t)
	conf.SystemConfig.HashIDSalt = ""

	// 格式不对
	for _, key := range []string{"", "a b", "a/b", "中文", strings.Repeat("a", 65)} {
		asserts.ErrorIs(CheckShareKeyAvailable(key, 2), ErrShareKeyInvalid, key)
	}

	// 普通自定义链接，未被占用
	{
		mock.ExpectQuery("SELECT(.+)custom_key(.+)").
			WithArgs("98k-win", 2).
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		asserts.NoError(CheckShareKeyAvailable("98k-win", 2))
		asserts.NoError(mock.ExpectationsWereMet())
	}

	// 已被另一个分享的自定义链接占用
	{
		mock.ExpectQuery("SELECT(.+)custom_key(.+)").
			WithArgs("98k-win", 2).
			WillReturnRows(sqlmock.NewRows([]string{"id", "source_name"}).AddRow(7, "old.exe"))
		err := CheckShareKeyAvailable("98k-win", 2)
		var taken ShareKeyTakenError
		asserts.ErrorAs(err, &taken)
		asserts.Equal("old.exe", taken.By.SourceName)
		asserts.NoError(mock.ExpectationsWereMet())
	}

	// 是仍在使用的分享 1 的 HashID
	{
		mock.ExpectQuery("SELECT(.+)").
			WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(1))
		var taken ShareKeyTakenError
		asserts.ErrorAs(CheckShareKeyAvailable("jRfM", 2), &taken)
		asserts.NoError(mock.ExpectationsWereMet())
	}

	// 是已删除分享 1 的 HashID：可以用
	{
		mock.ExpectQuery("SELECT(.+)").
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		mock.ExpectQuery("SELECT(.+)").
			WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(3))
		mock.ExpectQuery("SELECT(.+)custom_key(.+)").
			WithArgs("jRfM", 2).
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		asserts.NoError(CheckShareKeyAvailable("jRfM", 2))
		asserts.NoError(mock.ExpectationsWereMet())
	}

	// 是尚未分配的分享 ID 的 HashID：将来会被新分享抢走，不能用
	{
		mock.ExpectQuery("SELECT(.+)").
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		mock.ExpectQuery("SELECT(.+)").
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		asserts.ErrorIs(CheckShareKeyAvailable("jRfM", 2), ErrShareKeyInvalid)
		asserts.NoError(mock.ExpectationsWereMet())
	}

	// 就是本分享自己的 HashID
	{
		mock.ExpectQuery("SELECT(.+)custom_key(.+)").
			WithArgs("jRfM", 1).
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		asserts.NoError(CheckShareKeyAvailable("jRfM", 1))
		asserts.NoError(mock.ExpectationsWereMet())
	}
}

func TestShare_SetCustomKey(t *testing.T) {
	asserts := assert.New(t)
	conf.SystemConfig.HashIDSalt = ""

	// 设置
	{
		share := Share{Model: gorm.Model{ID: 2}}
		mock.ExpectQuery("SELECT(.+)custom_key(.+)").
			WithArgs("98k-win", 2).
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		mock.ExpectBegin()
		mock.ExpectExec("UPDATE(.+)custom_key(.+)").
			WithArgs("98k-win", sqlmock.AnyArg(), 2).
			WillReturnResult(sqlmock.NewResult(0, 1))
		mock.ExpectCommit()
		asserts.NoError(share.SetCustomKey("98k-win"))
		asserts.Equal("98k-win", share.Key())
		asserts.NoError(mock.ExpectationsWereMet())
	}

	// 设为自己的 HashID 等于清除，不做占用检查
	{
		share := Share{Model: gorm.Model{ID: 1}, CustomKey: "98k-win"}
		mock.ExpectBegin()
		mock.ExpectExec("UPDATE(.+)custom_key(.+)").
			WithArgs("", sqlmock.AnyArg(), 1).
			WillReturnResult(sqlmock.NewResult(0, 1))
		mock.ExpectCommit()
		asserts.NoError(share.SetCustomKey("jRfM"))
		asserts.Equal("", share.CustomKey)
		asserts.Equal("jRfM", share.Key())
		asserts.NoError(mock.ExpectationsWereMet())
	}

	// 被占用时不写数据库
	{
		share := Share{Model: gorm.Model{ID: 2}}
		mock.ExpectQuery("SELECT(.+)custom_key(.+)").
			WithArgs("98k-win", 2).
			WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(7))
		var taken ShareKeyTakenError
		asserts.ErrorAs(share.SetCustomKey("98k-win"), &taken)
		asserts.Equal("", share.CustomKey)
		asserts.NoError(mock.ExpectationsWereMet())
	}
}

func TestShare_IsAvailable(t *testing.T) {
	asserts := assert.New(t)

	// 下载剩余次数为0
	{
		share := Share{}
		asserts.False(share.IsAvailable())
	}

	// 时效过期
	{
		expires := time.Unix(10, 10)
		share := Share{
			RemainDownloads: -1,
			Expires:         &expires,
		}
		asserts.False(share.IsAvailable())
	}

	// 源对象为目录，但不存在
	{
		share := Share{
			RemainDownloads: -1,
			SourceID:        2,
			IsDir:           true,
		}
		mock.ExpectQuery("SELECT(.+)").
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		asserts.False(share.IsAvailable())
		asserts.NoError(mock.ExpectationsWereMet())
	}

	// 源对象为目录，存在
	{
		share := Share{
			RemainDownloads: -1,
			SourceID:        2,
			IsDir:           false,
		}
		mock.ExpectQuery("SELECT(.+)files(.+)").
			WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(13))
		asserts.True(share.IsAvailable())
		asserts.NoError(mock.ExpectationsWereMet())
	}

	// 用户被封禁
	{
		share := Share{
			RemainDownloads: -1,
			SourceID:        2,
			IsDir:           true,
			User:            User{Status: Baned},
		}
		asserts.False(share.IsAvailable())
	}
}

func TestShare_GetCreator(t *testing.T) {
	asserts := assert.New(t)

	mock.ExpectQuery("SELECT(.+)").WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(1))
	mock.ExpectQuery("SELECT(.+)").WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(1))
	share := Share{UserID: 1}
	res := share.Creator()
	asserts.NoError(mock.ExpectationsWereMet())
	asserts.EqualValues(1, res.ID)
}

func TestShare_Source(t *testing.T) {
	asserts := assert.New(t)

	// 目录
	{
		share := Share{IsDir: true, SourceID: 3}
		mock.ExpectQuery("SELECT(.+)").WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(3))
		asserts.EqualValues(3, share.Source().(*Folder).ID)
		asserts.NoError(mock.ExpectationsWereMet())
	}

	// 文件
	{
		share := Share{IsDir: false, SourceID: 3}
		mock.ExpectQuery("SELECT(.+)").WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(3))
		asserts.EqualValues(3, share.Source().(*File).ID)
		asserts.NoError(mock.ExpectationsWereMet())
	}
}

func TestShare_CanBeDownloadBy(t *testing.T) {
	asserts := assert.New(t)
	share := Share{}

	// 未登录，无权
	{
		user := &User{
			Group: Group{
				OptionsSerialized: GroupOption{
					ShareDownload: false,
				},
			},
		}
		asserts.Error(share.CanBeDownloadBy(user))
	}

	// 已登录，无权
	{
		user := &User{
			Model: gorm.Model{ID: 1},
			Group: Group{
				OptionsSerialized: GroupOption{
					ShareDownload: false,
				},
			},
		}
		asserts.Error(share.CanBeDownloadBy(user))
	}

	// 成功
	{
		user := &User{
			Model: gorm.Model{ID: 1},
			Group: Group{
				OptionsSerialized: GroupOption{
					ShareDownload: true,
				},
			},
		}
		asserts.NoError(share.CanBeDownloadBy(user))
	}
}

func TestShare_WasDownloadedBy(t *testing.T) {
	asserts := assert.New(t)
	share := Share{
		Model: gorm.Model{ID: 1},
	}

	// 已登录，已下载
	{
		user := User{
			Model: gorm.Model{
				ID: 1,
			},
		}
		r := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(r)
		cache.Set("share_1_1", true, 0)
		asserts.True(share.WasDownloadedBy(&user, c))
	}
}

func TestShare_DownloadBy(t *testing.T) {
	asserts := assert.New(t)
	share := Share{
		Model: gorm.Model{ID: 1},
	}
	user := User{
		Model: gorm.Model{
			ID: 1,
		},
	}
	cache.Deletes([]string{"1_1"}, "share_")
	r := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(r)

	mock.ExpectBegin()
	mock.ExpectExec("UPDATE(.+)").WillReturnResult(sqlmock.NewResult(1, 1))
	mock.ExpectCommit()

	err := share.DownloadBy(&user, c)
	asserts.NoError(mock.ExpectationsWereMet())
	asserts.NoError(err)
	_, ok := cache.Get("share_1_1")
	asserts.True(ok)
}

func TestShare_Viewed(t *testing.T) {
	asserts := assert.New(t)
	share := Share{}
	mock.ExpectBegin()
	mock.ExpectExec("UPDATE(.+)").
		WillReturnResult(sqlmock.NewResult(1, 1))
	mock.ExpectCommit()
	share.Viewed()
	asserts.NoError(mock.ExpectationsWereMet())
	asserts.EqualValues(1, share.Views)
}

func TestShare_UpdateAndDelete(t *testing.T) {
	asserts := assert.New(t)
	share := Share{}

	{
		mock.ExpectBegin()
		mock.ExpectExec("UPDATE(.+)").
			WillReturnResult(sqlmock.NewResult(1, 1))
		mock.ExpectCommit()
		err := share.Update(map[string]interface{}{"id": 1})
		asserts.NoError(mock.ExpectationsWereMet())
		asserts.NoError(err)
	}

	{
		mock.ExpectBegin()
		mock.ExpectExec("UPDATE(.+)").
			WillReturnResult(sqlmock.NewResult(1, 1))
		mock.ExpectCommit()
		err := share.Delete()
		asserts.NoError(mock.ExpectationsWereMet())
		asserts.NoError(err)
	}

	{
		mock.ExpectBegin()
		mock.ExpectExec("UPDATE(.+)").
			WillReturnResult(sqlmock.NewResult(1, 1))
		mock.ExpectCommit()
		err := DeleteShareBySourceIDs([]uint{1}, true)
		asserts.NoError(mock.ExpectationsWereMet())
		asserts.NoError(err)
	}

}

func TestListShares(t *testing.T) {
	asserts := assert.New(t)

	mock.ExpectQuery("SELECT(.+)").WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(2).AddRow(2))
	mock.ExpectQuery("SELECT(.+)").WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(1).AddRow(2))

	res, total := ListShares(1, 1, 10, "desc", true)
	asserts.NoError(mock.ExpectationsWereMet())
	asserts.Len(res, 2)
	asserts.Equal(2, total)
}

func TestSearchShares(t *testing.T) {
	asserts := assert.New(t)

	mock.ExpectQuery("SELECT(.+)").WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(1))
	mock.ExpectQuery("SELECT(.+)").
		WithArgs("", sqlmock.AnyArg(), "%1%2%").
		WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(1))
	res, total := SearchShares(1, 10, "id", "1 2")
	asserts.NoError(mock.ExpectationsWereMet())
	asserts.Len(res, 1)
	asserts.Equal(1, total)
}
